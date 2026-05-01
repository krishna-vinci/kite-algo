from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.worker_protection import validate_backend_protection_payload
from api.worker_market_data import (
    WorkerInstrumentResolveRequest,
    WorkerMarketDataService,
    WorkerMarketSnapshotRequest,
    WorkerQuoteRequest,
    normalize_instrument_token,
)
from algo_runtime.account_scope import parse_account_scope
from algo_runtime.execution_attribution import build_execution_attribution, build_paper_execution_attribution
from auth_service import require_app_user
from database import SessionLocal
from journaling.service import JournalService


router = APIRouter(prefix="/algo-workers", tags=["Algo Workers"])
logger = logging.getLogger(__name__)

DEFAULT_WORKER_ACTIONS = {
    "runs:create",
    "runs:read",
    "intents:submit",
    "risk:update",
    "runs:exit",
    "heartbeat",
    "market:read",
    "market:stream",
    "funds:read",
}
ALLOWED_V1_MODES = {"paper", "dry_run", "live"}
LIVE_REQUIRED_RUN_METADATA = {"strategy_family", "strategy_name"}
VALID_WORKER_STRATEGY_FAMILIES = {
    "options_strategy",
    "indicator_strategy",
    "investment_strategy",
    "discretionary_strategy",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=_json_default)


def _json_loads(value: Any, fallback: Any) -> Any:
    import json

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


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
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


class WorkerTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    account_scope: Optional[str] = None
    allowed_modes: List[str] = Field(default_factory=lambda: ["paper", "dry_run"])
    allowed_actions: List[str] = Field(default_factory=lambda: sorted(DEFAULT_WORKER_ACTIONS))
    allowed_templates: List[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_modes", "allowed_actions", "allowed_templates")
    @classmethod
    def _clean_list(cls, value: List[str]) -> List[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("allowed_modes")
    @classmethod
    def _normalize_modes(cls, value: List[str]) -> List[str]:
        return [item.lower() for item in value]


class WorkerTokenCreateResponse(BaseModel):
    token_id: str
    token: str
    name: str
    account_scope: Optional[str]
    allowed_modes: List[str]
    allowed_actions: List[str]
    allowed_templates: List[str]
    expires_at: Optional[datetime]


class WorkerTokenView(BaseModel):
    token_id: str
    name: str
    account_scope: Optional[str]
    allowed_modes: List[str]
    allowed_actions: List[str]
    allowed_templates: List[str]
    status: str
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class WorkerHeartbeatRequest(BaseModel):
    worker_id: Optional[str] = None
    status: str = "healthy"
    metrics: Dict[str, Any] = Field(default_factory=dict)


class WorkerRunCreateRequest(BaseModel):
    strategy_run_id: Optional[str] = None
    template_id: str = Field(min_length=1)
    account_scope: str = Field(min_length=1)
    execution_mode: str = "paper"
    summary_fields: List[Dict[str, Any]] = Field(default_factory=list)
    risk_schema: List[Dict[str, Any]] = Field(default_factory=list)
    allowed_actions: List[str] = Field(default_factory=lambda: ["edit_risk", "exit_strategy"])
    runtime_state: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("execution_mode")
    @classmethod
    def _clean_mode(cls, value: str) -> str:
        return str(value or "paper").strip().lower()


class WorkerRiskPatchRequest(BaseModel):
    patch: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None


class WorkerProtectionPatchRequest(BaseModel):
    backend_protection: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None
    reset_trailing: bool = True


class WorkerIntentRequest(BaseModel):
    intent_type: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=160)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkerExitRequest(BaseModel):
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    dry_run: bool = False


class WorkerOrderActionRequest(BaseModel):
    strategy_run_id: str = Field(min_length=1)
    variety: str = "regular"
    parent_order_id: Optional[str] = None


class WorkerOrderModifyRequest(WorkerOrderActionRequest):
    order_type: Optional[str] = None
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    quantity: Optional[int] = Field(None, gt=0)
    validity: Optional[str] = None
    validity_ttl: Optional[int] = None

    def to_modify_request(self) -> Any:
        from broker_api.kite_orders import ModifyOrderRequest

        return ModifyOrderRequest.model_validate(
            self.model_dump(
                exclude_none=True,
                exclude={"strategy_run_id", "variety", "parent_order_id"},
            )
        )


class WorkerOrderPreviewRequest(BaseModel):
    order: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkerBasketPreviewRequest(BaseModel):
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    all_or_none: bool = False
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkerRunPnlTotals(BaseModel):
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0


class WorkerRunPnlLeg(BaseModel):
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: Optional[str] = None
    net_quantity: int = 0
    side: str = "FLAT"
    average_price: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    broker_net_quantity: Optional[int] = None
    is_stale: bool = False
    last_reconciled_at: Optional[str] = None


class WorkerRunPnlSnapshot(BaseModel):
    strategy_run_id: str
    execution_mode: str
    status: str
    currency: str = "INR"
    totals: WorkerRunPnlTotals = Field(default_factory=WorkerRunPnlTotals)
    legs: List[WorkerRunPnlLeg] = Field(default_factory=list)
    position_count: int = 0
    is_realtime: bool = False
    is_stale: bool = False
    updated_at: str


class WorkerFundsSegment(BaseModel):
    net: float = 0.0
    available_cash: float = 0.0
    opening_balance: float = 0.0
    live_balance: Optional[float] = None
    collateral: Optional[float] = None
    utilised: float = 0.0
    m2m_realised: float = 0.0
    m2m_unrealised: float = 0.0


class WorkerFundsSnapshot(BaseModel):
    account_scope: str
    mode: str
    currency: str = "INR"
    source: str
    segments: Dict[str, WorkerFundsSegment] = Field(default_factory=dict)
    allocation: Dict[str, Any] = Field(default_factory=dict)
    stale: bool = False
    updated_at: str


@dataclass
class WorkerToken:
    token_id: str
    name: str
    account_scope: Optional[str]
    allowed_modes: List[str]
    allowed_actions: List[str]
    allowed_templates: List[str]
    status: str = "active"
    expires_at: Optional[datetime] = None


class SqlAlchemyAlgoWorkerRepository:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    async def create_token(self, payload: WorkerTokenCreateRequest, *, raw_token: str, token_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._create_token_sync, payload, raw_token, token_id)

    async def list_tokens(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_tokens_sync)

    async def revoke_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._revoke_token_sync, token_id)

    async def get_token_by_hash(self, token_hash: str) -> Optional[WorkerToken]:
        return await asyncio.to_thread(self._get_token_by_hash_sync, token_hash)

    async def touch_token(self, token_id: str) -> None:
        await asyncio.to_thread(self._touch_token_sync, token_id)

    async def record_heartbeat(self, token_id: str, payload: WorkerHeartbeatRequest) -> Dict[str, Any]:
        return await asyncio.to_thread(self._record_heartbeat_sync, token_id, payload)

    async def create_run(self, token: WorkerToken, payload: WorkerRunCreateRequest, *, strategy_run_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._create_run_sync, token, payload, strategy_run_id)

    async def get_run(self, strategy_run_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_run_sync, strategy_run_id)

    async def list_runs_for_control_plane(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_runs_for_control_plane_sync)

    async def list_protection_enabled_runs(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_protection_enabled_runs_sync)

    async def update_run_risk(self, strategy_run_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._update_run_risk_sync, strategy_run_id, patch)

    async def update_run_runtime_state(self, strategy_run_id: str, runtime_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._update_run_runtime_state_sync, strategy_run_id, runtime_state)

    async def update_run_backend_protection(
        self,
        strategy_run_id: str,
        protection: Dict[str, Any],
        protection_state: Dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._update_run_backend_protection_sync,
            strategy_run_id,
            protection,
            protection_state,
            expected_generation,
            expected_triggered_rule,
            expected_exit_claim_id,
        )

    async def update_run_backend_protection_state(
        self,
        strategy_run_id: str,
        protection_state: Dict[str, Any],
        *,
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._update_run_backend_protection_state_sync,
            strategy_run_id,
            protection_state,
            expected_generation,
            expected_triggered_rule,
            expected_exit_claim_id,
        )

    async def update_run_status(self, strategy_run_id: str, status: str, *, state_patch: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._update_run_status_sync, strategy_run_id, status, state_patch)

    async def list_live_strategy_open_legs(self, *, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_live_strategy_open_legs_sync, strategy_run_id, account_id)

    async def get_live_order_attribution_refs(self, *, strategy_run_id: str, account_id: str) -> Dict[str, List[str]]:
        return await asyncio.to_thread(self._get_live_order_attribution_refs_sync, strategy_run_id, account_id)

    async def list_live_strategy_broker_positions(self, *, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._list_live_strategy_broker_positions_sync, strategy_run_id, account_id)

    async def get_intent_result(self, strategy_run_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_intent_result_sync, strategy_run_id, idempotency_key)

    async def save_intent_result(self, *, token_id: str, strategy_run_id: str, request: WorkerIntentRequest, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._save_intent_result_sync,
            token_id=token_id,
            strategy_run_id=strategy_run_id,
            request=request,
            status=status,
            result=result,
        )

    def _create_token_sync(self, payload: WorkerTokenCreateRequest, raw_token: str, token_id: str) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_tokens (
                        token_id, name, token_hash, account_scope, allowed_modes,
                        allowed_actions, allowed_templates, status, expires_at, metadata_json
                    ) VALUES (
                        :token_id, :name, :token_hash, :account_scope, CAST(:allowed_modes AS JSONB),
                        CAST(:allowed_actions AS JSONB), CAST(:allowed_templates AS JSONB),
                        'active', :expires_at, CAST(:metadata_json AS JSONB)
                    )
                    RETURNING token_id, name, account_scope, allowed_modes, allowed_actions,
                              allowed_templates, status, created_at, expires_at, last_used_at
                    """
                ),
                {
                    "token_id": token_id,
                    "name": payload.name,
                    "token_hash": _hash_token(raw_token),
                    "account_scope": payload.account_scope,
                    "allowed_modes": _json_dumps(payload.allowed_modes),
                    "allowed_actions": _json_dumps(payload.allowed_actions),
                    "allowed_templates": _json_dumps(payload.allowed_templates),
                    "expires_at": payload.expires_at,
                    "metadata_json": _json_dumps(payload.metadata),
                },
            ).fetchone()
            db.commit()
            return self._token_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _list_tokens_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT token_id, name, account_scope, allowed_modes, allowed_actions,
                           allowed_templates, status, created_at, expires_at, last_used_at
                    FROM public.algo_worker_tokens
                    ORDER BY created_at DESC
                    """
                )
            ).fetchall()
            return [self._token_view(row) for row in rows]
        finally:
            db.close()

    def _revoke_token_sync(self, token_id: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_tokens
                    SET status = 'revoked', updated_at = NOW()
                    WHERE token_id = :token_id
                    RETURNING token_id, name, account_scope, allowed_modes, allowed_actions,
                              allowed_templates, status, created_at, expires_at, last_used_at
                    """
                ),
                {"token_id": token_id},
            ).fetchone()
            db.commit()
            return self._token_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get_token_by_hash_sync(self, token_hash: str) -> Optional[WorkerToken]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT token_id, name, account_scope, allowed_modes, allowed_actions,
                           allowed_templates, status, expires_at
                    FROM public.algo_worker_tokens
                    WHERE token_hash = :token_hash
                    """
                ),
                {"token_hash": token_hash},
            ).fetchone()
            if row is None:
                return None
            payload = _row_mapping(row)
            return WorkerToken(
                token_id=str(payload["token_id"]),
                name=str(payload["name"]),
                account_scope=payload.get("account_scope"),
                allowed_modes=_json_loads(payload.get("allowed_modes"), []),
                allowed_actions=_json_loads(payload.get("allowed_actions"), []),
                allowed_templates=_json_loads(payload.get("allowed_templates"), []),
                status=str(payload.get("status") or "active"),
                expires_at=payload.get("expires_at"),
            )
        finally:
            db.close()

    def _touch_token_sync(self, token_id: str) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text("UPDATE public.algo_worker_tokens SET last_used_at = NOW(), updated_at = NOW() WHERE token_id = :token_id"),
                {"token_id": token_id},
            )
            db.commit()
        finally:
            db.close()

    def _record_heartbeat_sync(self, token_id: str, payload: WorkerHeartbeatRequest) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    UPDATE public.algo_worker_tokens
                    SET last_heartbeat_at = NOW(),
                        heartbeat_json = CAST(:heartbeat_json AS JSONB),
                        updated_at = NOW()
                    WHERE token_id = :token_id
                    """
                ),
                {
                    "token_id": token_id,
                    "heartbeat_json": _json_dumps(payload.model_dump(mode="json")),
                },
            )
            db.commit()
            return {"status": "ok", "token_id": token_id, "received_at": _utcnow().isoformat()}
        finally:
            db.close()

    def _create_run_sync(self, token: WorkerToken, payload: WorkerRunCreateRequest, strategy_run_id: str) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode,
                        status, summary_fields_json, risk_schema_json, allowed_actions_json,
                        runtime_state_json, metadata_json
                    ) VALUES (
                        :strategy_run_id, :token_id, :template_id, :account_scope, :execution_mode,
                        'open', CAST(:summary_fields_json AS JSONB), CAST(:risk_schema_json AS JSONB),
                        CAST(:allowed_actions_json AS JSONB), CAST(:runtime_state_json AS JSONB),
                        CAST(:metadata_json AS JSONB)
                    )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "token_id": token.token_id,
                    "template_id": payload.template_id,
                    "account_scope": payload.account_scope,
                    "execution_mode": payload.execution_mode,
                    "summary_fields_json": _json_dumps(payload.summary_fields),
                    "risk_schema_json": _json_dumps(payload.risk_schema),
                    "allowed_actions_json": _json_dumps(payload.allowed_actions),
                    "runtime_state_json": _json_dumps(payload.runtime_state),
                    "metadata_json": _json_dumps(payload.metadata),
                },
            ).fetchone()
            db.commit()
            return self._run_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get_run_sync(self, strategy_run_id: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(text("SELECT * FROM public.algo_worker_runs WHERE strategy_run_id = :strategy_run_id"), {"strategy_run_id": strategy_run_id}).fetchone()
            return self._run_view(row) if row else None
        finally:
            db.close()

    def _list_runs_for_control_plane_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        r.*,
                        t.name AS worker_name,
                        t.last_heartbeat_at,
                        t.heartbeat_json
                    FROM public.algo_worker_runs r
                    LEFT JOIN public.algo_worker_tokens t ON t.token_id = r.token_id
                    ORDER BY COALESCE(r.updated_at, r.created_at) DESC
                    """
                )
            ).fetchall()
            return [self._run_view_with_worker(row) for row in rows]
        finally:
            db.close()

    def _list_protection_enabled_runs_sync(self) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        r.*,
                        t.name AS worker_name,
                        t.last_heartbeat_at,
                        t.heartbeat_json
                    FROM public.algo_worker_runs r
                    LEFT JOIN public.algo_worker_tokens t ON t.token_id = r.token_id
                    WHERE r.status IN ('open', 'exiting')
                      AND COALESCE((r.runtime_state_json -> 'backend_protection' ->> 'enabled')::BOOLEAN, FALSE) = TRUE
                    ORDER BY COALESCE(r.updated_at, r.created_at) ASC
                    """
                )
            ).fetchall()
            return [self._run_view_with_worker(row) for row in rows]
        finally:
            db.close()

    def _update_run_risk_sync(self, strategy_run_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        run = self._get_run_sync(strategy_run_id)
        if run is None:
            raise KeyError(strategy_run_id)
        state = dict(run.get("runtime_state") or {})
        risk = dict(state.get("risk") or {})
        risk.update(patch)
        state["risk"] = risk

        risk_schema = []
        for field in list(run.get("risk_schema") or []):
            item = dict(field)
            if item.get("key") in patch:
                item["value"] = patch[item["key"]]
            risk_schema.append(item)

        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        risk_schema_json = CAST(:risk_schema_json AS JSONB),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "runtime_state_json": _json_dumps(state),
                    "risk_schema_json": _json_dumps(risk_schema),
                },
            ).fetchone()
            db.commit()
            return self._run_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_status_sync(self, strategy_run_id: str, status: str, state_patch: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        run = self._get_run_sync(strategy_run_id)
        if run is None:
            return None
        state = dict(run.get("runtime_state") or {})
        if state_patch:
            state.update(state_patch)
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET status = :status,
                        runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        updated_at = NOW(),
                        closed_at = CASE WHEN :status = 'closed' THEN NOW() ELSE closed_at END
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {"strategy_run_id": strategy_run_id, "status": status, "runtime_state_json": _json_dumps(state)},
            ).fetchone()
            db.commit()
            return self._run_view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_runtime_state_sync(self, strategy_run_id: str, runtime_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = CAST(:runtime_state_json AS JSONB),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                    RETURNING *
                    """
                ),
                {"strategy_run_id": strategy_run_id, "runtime_state_json": _json_dumps(runtime_state)},
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_backend_protection_sync(
        self,
        strategy_run_id: str,
        protection: Dict[str, Any],
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = jsonb_set(
                            jsonb_set(
                                COALESCE(runtime_state_json, '{}'::jsonb),
                                '{backend_protection}',
                                CAST(:protection_json AS JSONB),
                                true
                            ),
                            '{backend_protection_state}',
                            CAST(:protection_state_json AS JSONB),
                            true
                        ),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND (
                        :expected_generation IS NULL
                        OR COALESCE((runtime_state_json -> 'backend_protection_state' ->> 'generation')::INTEGER, 0) = :expected_generation
                      )
                      AND (
                        :check_triggered_rule = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'triggered_rule', '') = :expected_triggered_rule
                      )
                      AND (
                        :check_exit_claim_id = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'exit_claim_id', '') = :expected_exit_claim_id
                      )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "protection_json": _json_dumps(protection),
                    "protection_state_json": _json_dumps(protection_state),
                    "expected_generation": expected_generation,
                    "check_triggered_rule": expected_triggered_rule is not None,
                    "expected_triggered_rule": expected_triggered_rule or "",
                    "check_exit_claim_id": expected_exit_claim_id is not None,
                    "expected_exit_claim_id": expected_exit_claim_id or "",
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_run_backend_protection_state_sync(
        self,
        strategy_run_id: str,
        protection_state: Dict[str, Any],
        expected_generation: Optional[int] = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_worker_runs
                    SET runtime_state_json = jsonb_set(
                            COALESCE(runtime_state_json, '{}'::jsonb),
                            '{backend_protection_state}',
                            CAST(:protection_state_json AS JSONB),
                            true
                        ),
                        updated_at = NOW()
                    WHERE strategy_run_id = :strategy_run_id
                      AND (
                        :expected_generation IS NULL
                        OR COALESCE((runtime_state_json -> 'backend_protection_state' ->> 'generation')::INTEGER, 0) = :expected_generation
                      )
                      AND (
                        :check_triggered_rule = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'triggered_rule', '') = :expected_triggered_rule
                      )
                      AND (
                        :check_exit_claim_id = FALSE
                        OR COALESCE(runtime_state_json -> 'backend_protection_state' ->> 'exit_claim_id', '') = :expected_exit_claim_id
                      )
                    RETURNING *
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "protection_state_json": _json_dumps(protection_state),
                    "expected_generation": expected_generation,
                    "check_triggered_rule": expected_triggered_rule is not None,
                    "expected_triggered_rule": expected_triggered_rule or "",
                    "check_exit_claim_id": expected_exit_claim_id is not None,
                    "expected_exit_claim_id": expected_exit_claim_id or "",
                },
            ).fetchone()
            db.commit()
            return self._run_view(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _list_live_strategy_open_legs_sync(self, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    WITH attributed_orders AS (
                        SELECT DISTINCT
                            resolved.account_id,
                            resolved.broker_order_id
                        FROM (
                            SELECT
                                loi.account_id,
                                loi.broker_order_id
                            FROM public.live_order_intents loi
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.broker_order_id, ''), '') <> ''
                            UNION ALL
                            SELECT
                                loi.account_id,
                                coe.order_id AS broker_order_id
                            FROM public.live_order_intents loi
                            INNER JOIN public.canonical_order_events coe
                              ON coe.account_id = loi.account_id
                             AND CAST(coe.payload_json AS TEXT) LIKE '%"tag"%'
                             AND CAST(coe.payload_json AS TEXT) LIKE '%' || '"' || loi.client_order_ref || '"' || '%'
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.client_order_ref, ''), '') <> ''
                        ) resolved
                        WHERE COALESCE(NULLIF(resolved.broker_order_id, ''), '') <> ''
                    ),
                    leg_facts AS (
                        SELECT
                            CAST(NULL AS UUID) AS journal_run_id,
                            otf.account_id,
                            otf.instrument_token,
                            COALESCE(NULLIF(otf.exchange, ''), COALESCE(otf.payload_json ->> 'exchange', '')) AS exchange,
                            COALESCE(NULLIF(otf.tradingsymbol, ''), COALESCE(otf.payload_json ->> 'tradingsymbol', '')) AS tradingsymbol,
                            COALESCE(NULLIF(otf.product, ''), COALESCE(otf.payload_json ->> 'product', '')) AS product,
                            SUM(
                                CASE
                                    WHEN UPPER(COALESCE(NULLIF(otf.transaction_type, ''), COALESCE(otf.payload_json ->> 'transaction_type', ''))) = 'BUY'
                                        THEN otf.quantity
                                    ELSE -otf.quantity
                                END
                            ) AS net_quantity
                        FROM public.order_trade_fills otf
                        INNER JOIN attributed_orders ao
                          ON ao.account_id = otf.account_id
                         AND ao.broker_order_id = otf.order_id
                        GROUP BY
                            otf.account_id,
                            otf.instrument_token,
                            COALESCE(NULLIF(otf.exchange, ''), COALESCE(otf.payload_json ->> 'exchange', '')),
                            COALESCE(NULLIF(otf.tradingsymbol, ''), COALESCE(otf.payload_json ->> 'tradingsymbol', '')),
                            COALESCE(NULLIF(otf.product, ''), COALESCE(otf.payload_json ->> 'product', ''))
                    )
                    SELECT
                        lf.journal_run_id,
                        lf.account_id,
                        lf.instrument_token,
                        lf.exchange,
                        lf.tradingsymbol,
                        lf.product,
                        lf.net_quantity,
                        ap.net_quantity AS broker_net_quantity
                    FROM leg_facts lf
                    LEFT JOIN public.account_positions ap
                      ON ap.account_id = lf.account_id
                      AND ap.instrument_token = lf.instrument_token
                      AND ap.product = lf.product
                      AND ap.exchange = lf.exchange
                      AND ap.tradingsymbol = lf.tradingsymbol
                    WHERE lf.net_quantity <> 0
                    ORDER BY lf.exchange, lf.tradingsymbol, lf.product
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            return [dict(row) for row in rows]
        finally:
            db.close()

    def _get_live_order_attribution_refs_sync(self, strategy_run_id: str, account_id: str) -> Dict[str, List[str]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT client_order_ref, broker_order_id
                    FROM public.live_order_intents
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                    ORDER BY created_at DESC
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            recovered_rows = db.execute(
                text(
                    """
                    SELECT DISTINCT
                        loi.client_order_ref,
                        coe.order_id AS broker_order_id
                    FROM public.live_order_intents loi
                    INNER JOIN public.canonical_order_events coe
                      ON coe.account_id = loi.account_id
                     AND CAST(coe.payload_json AS TEXT) LIKE '%"tag"%'
                     AND CAST(coe.payload_json AS TEXT) LIKE '%' || '"' || loi.client_order_ref || '"' || '%'
                    WHERE loi.strategy_run_id = :strategy_run_id
                      AND loi.account_id = :account_id
                      AND COALESCE(NULLIF(loi.client_order_ref, ''), '') <> ''
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            combined_rows = [dict(row) for row in rows] + [dict(row) for row in recovered_rows]
            broker_order_ids = sorted(
                {
                    str(row.get("broker_order_id") or "").strip()
                    for row in combined_rows
                    if str(row.get("broker_order_id") or "").strip()
                }
            )
            client_order_refs = sorted(
                {
                    str(row.get("client_order_ref") or "").strip()
                    for row in combined_rows
                    if str(row.get("client_order_ref") or "").strip()
                }
            )
            return {
                "broker_order_ids": broker_order_ids,
                "client_order_refs": client_order_refs,
            }
        finally:
            db.close()

    def _list_live_strategy_broker_positions_sync(self, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    WITH attributed_orders AS (
                        SELECT DISTINCT
                            resolved.account_id,
                            resolved.broker_order_id
                        FROM (
                            SELECT
                                loi.account_id,
                                loi.broker_order_id
                            FROM public.live_order_intents loi
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.broker_order_id, ''), '') <> ''
                            UNION ALL
                            SELECT
                                loi.account_id,
                                coe.order_id AS broker_order_id
                            FROM public.live_order_intents loi
                            INNER JOIN public.canonical_order_events coe
                              ON coe.account_id = loi.account_id
                             AND CAST(coe.payload_json AS TEXT) LIKE '%"tag"%'
                             AND CAST(coe.payload_json AS TEXT) LIKE '%' || '"' || loi.client_order_ref || '"' || '%'
                            WHERE loi.strategy_run_id = :strategy_run_id
                              AND loi.account_id = :account_id
                              AND COALESCE(NULLIF(loi.client_order_ref, ''), '') <> ''
                        ) resolved
                        WHERE COALESCE(NULLIF(resolved.broker_order_id, ''), '') <> ''
                    ),
                    attributed_instruments AS (
                        SELECT DISTINCT
                            osp.account_id,
                            osp.instrument_token,
                            osp.exchange,
                            osp.tradingsymbol,
                            osp.product
                        FROM public.order_state_projection osp
                        INNER JOIN attributed_orders ao
                          ON ao.account_id = osp.account_id
                         AND ao.broker_order_id = osp.order_id
                        WHERE COALESCE(osp.instrument_token, 0) <> 0
                          AND COALESCE(NULLIF(osp.exchange, ''), '') <> ''
                          AND COALESCE(NULLIF(osp.tradingsymbol, ''), '') <> ''
                          AND COALESCE(NULLIF(osp.product, ''), '') <> ''
                    )
                    SELECT
                        ap.account_id,
                        ap.instrument_token,
                        ap.exchange,
                        ap.tradingsymbol,
                        ap.product,
                        ap.net_quantity,
                        ap.average_price,
                        ap.last_price,
                        ap.updated_at,
                        ap.last_reconciled_at
                    FROM public.account_positions ap
                    INNER JOIN attributed_instruments ai
                      ON ai.account_id = ap.account_id
                     AND ai.instrument_token = ap.instrument_token
                     AND ai.exchange = ap.exchange
                     AND ai.tradingsymbol = ap.tradingsymbol
                     AND ai.product = ap.product
                    WHERE ap.net_quantity <> 0
                    ORDER BY ap.exchange, ap.tradingsymbol, ap.product
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).mappings().all()
            return [dict(row) for row in rows]
        finally:
            db.close()

    def _get_intent_result_sync(self, strategy_run_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT result_json
                    FROM public.algo_worker_intents
                    WHERE strategy_run_id = :strategy_run_id AND idempotency_key = :idempotency_key
                    """
                ),
                {"strategy_run_id": strategy_run_id, "idempotency_key": idempotency_key},
            ).fetchone()
            if row is None:
                return None
            return _json_loads(_row_mapping(row).get("result_json"), {})
        finally:
            db.close()

    def _save_intent_result_sync(self, *, token_id: str, strategy_run_id: str, request: WorkerIntentRequest, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_intents (
                        token_id, strategy_run_id, idempotency_key, intent_type,
                        request_json, status, result_json
                    ) VALUES (
                        :token_id, :strategy_run_id, :idempotency_key, :intent_type,
                        CAST(:request_json AS JSONB), :status, CAST(:result_json AS JSONB)
                    )
                    ON CONFLICT (strategy_run_id, idempotency_key) DO UPDATE SET
                        result_json = public.algo_worker_intents.result_json
                    RETURNING result_json
                    """
                ),
                {
                    "token_id": token_id,
                    "strategy_run_id": strategy_run_id,
                    "idempotency_key": request.idempotency_key,
                    "intent_type": request.intent_type,
                    "request_json": request.model_dump_json(),
                    "status": status,
                    "result_json": _json_dumps(result),
                },
            ).fetchone()
            db.commit()
            return _json_loads(_row_mapping(row).get("result_json"), result)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _token_view(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "token_id": str(payload["token_id"]),
            "name": str(payload["name"]),
            "account_scope": payload.get("account_scope"),
            "allowed_modes": _json_loads(payload.get("allowed_modes"), []),
            "allowed_actions": _json_loads(payload.get("allowed_actions"), []),
            "allowed_templates": _json_loads(payload.get("allowed_templates"), []),
            "status": str(payload.get("status") or "active"),
            "created_at": payload.get("created_at"),
            "expires_at": payload.get("expires_at"),
            "last_used_at": payload.get("last_used_at"),
        }

    def _run_view(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "strategy_run_id": str(payload["strategy_run_id"]),
            "token_id": str(payload["token_id"]),
            "template_id": str(payload["template_id"]),
            "account_scope": str(payload["account_scope"]),
            "execution_mode": str(payload["execution_mode"]),
            "status": str(payload.get("status") or "open"),
            "summary_fields": _json_loads(payload.get("summary_fields_json"), []),
            "risk_schema": _json_loads(payload.get("risk_schema_json"), []),
            "allowed_actions": _json_loads(payload.get("allowed_actions_json"), []),
            "runtime_state": _json_loads(payload.get("runtime_state_json"), {}),
            "metadata": _json_loads(payload.get("metadata_json"), {}),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "closed_at": payload.get("closed_at"),
        }

    def _run_view_with_worker(self, row: Any) -> Dict[str, Any]:
        payload = self._run_view(row)
        raw = _row_mapping(row)
        payload["worker_name"] = raw.get("worker_name")
        payload["last_heartbeat_at"] = raw.get("last_heartbeat_at")
        payload["heartbeat_json"] = _json_loads(raw.get("heartbeat_json"), {})
        return payload


def _repo(request: Any) -> Any:
    repository = getattr(request.app.state, "algo_worker_repository", None)
    if repository is None:
        repository = SqlAlchemyAlgoWorkerRepository()
        request.app.state.algo_worker_repository = repository
    return repository


def _market_data_service(request: Any) -> WorkerMarketDataService:
    service = getattr(request.app.state, "worker_market_data_service", None)
    if service is not None:
        return service
    candle_reader = getattr(request.app.state, "worker_candle_data_reader", None)
    if candle_reader is None:
        candle_reader = getattr(request.app.state, "algo_worker_candle_reader", None)
    if candle_reader is None:
        try:
            from algo_runtime.snapshot_builder import RedisCandleDataReader
            from broker_api.candle_aggregator import INTERVAL_SECONDS
            from broker_api.candle_storage import CandleStorage
            from broker_api.redis_events import get_redis

            candle_reader = RedisCandleDataReader(
                redis_client=get_redis(),
                candle_storage=CandleStorage,
                interval_seconds=INTERVAL_SECONDS,
            )
            request.app.state.algo_worker_candle_reader = candle_reader
        except Exception:
            candle_reader = None
    return WorkerMarketDataService(
        market_data_runtime=getattr(request.app.state, "market_data_runtime", None),
        redis=getattr(getattr(request.app.state, "market_data_runtime", None), "redis", None),
        candle_reader=candle_reader,
    )


def _journal_service(request: Any) -> JournalService:
    service = getattr(request.app.state, "journal_service", None)
    if service is None:
        service = JournalService()
        request.app.state.journal_service = service
    return service


def _parse_csv_values(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _parse_csv_int_values(value: Optional[str], *, field_name: str) -> List[int]:
    parsed: List[int] = []
    for item in _parse_csv_values(value):
        try:
            numeric = int(item)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"{field_name} must contain comma-separated integers") from None
        if numeric <= 0 or numeric > 9_999_999_999:
            raise HTTPException(status_code=422, detail=f"{field_name} contains an out-of-range instrument token")
        parsed.append(numeric)
    return parsed


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Worker bearer token required")
    return token.strip()


def _extract_ws_token(websocket: WebSocket) -> str:
    token = str(websocket.query_params.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Worker websocket token required")
    return token


async def require_worker_token(request: Request) -> WorkerToken:
    raw_token = _extract_bearer_token(request)
    repository = _repo(request)
    token = await repository.get_token_by_hash(_hash_token(raw_token))
    if token is None or token.status != "active":
        raise HTTPException(status_code=401, detail="Invalid worker token")
    if token.expires_at and token.expires_at <= _utcnow():
        raise HTTPException(status_code=401, detail="Worker token expired")
    await repository.touch_token(token.token_id)
    return token


async def require_worker_ws_token(websocket: WebSocket) -> WorkerToken:
    raw_token = _extract_ws_token(websocket)
    repository = _repo(websocket)
    token = await repository.get_token_by_hash(_hash_token(raw_token))
    if token is None or token.status != "active":
        raise HTTPException(status_code=401, detail="Invalid worker token")
    if token.expires_at and token.expires_at <= _utcnow():
        raise HTTPException(status_code=401, detail="Worker token expired")
    await repository.touch_token(token.token_id)
    return token


def _serialize_model(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


def _payload_matches_strategy_run(payload: Dict[str, Any], strategy_run_id: str) -> bool:
    if str(payload.get("strategy_run_id") or "") == strategy_run_id:
        return True
    attribution = payload.get("attribution")
    if isinstance(attribution, dict) and str(attribution.get("strategy_run_id") or "") == strategy_run_id:
        return True
    meta = payload.get("meta")
    if isinstance(meta, dict) and str(meta.get("strategy_run_id") or "") == strategy_run_id:
        return True
    return False


def _payload_matches_live_attribution(payload: Dict[str, Any], refs: Dict[str, List[str]]) -> bool:
    broker_order_ids = {str(value).strip() for value in refs.get("broker_order_ids") or [] if str(value).strip()}
    client_order_refs = {str(value).strip() for value in refs.get("client_order_refs") or [] if str(value).strip()}

    order_id_candidates = {
        str(payload.get("order_id") or "").strip(),
        str(payload.get("broker_order_id") or "").strip(),
    }
    if broker_order_ids.intersection({value for value in order_id_candidates if value}):
        return True

    tag_candidates: set[str] = set()
    tag_value = payload.get("tag")
    if tag_value is not None:
        cleaned = str(tag_value).strip()
        if cleaned:
            tag_candidates.add(cleaned)
    tags_value = payload.get("tags")
    if isinstance(tags_value, list):
        tag_candidates.update(str(item).strip() for item in tags_value if str(item).strip())
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in ("tag", "client_order_ref"):
            cleaned = str(meta.get(key) or "").strip()
            if cleaned:
                tag_candidates.add(cleaned)
    attribution = payload.get("attribution")
    if isinstance(attribution, dict):
        cleaned = str(attribution.get("client_order_ref") or "").strip()
        if cleaned:
            tag_candidates.add(cleaned)
    if client_order_refs.intersection(tag_candidates):
        return True

    return False


async def _worker_run_live_attribution_refs(request: Request, run: Dict[str, Any]) -> Dict[str, List[str]]:
    return await _repo(request).get_live_order_attribution_refs(
        strategy_run_id=str(run["strategy_run_id"]),
        account_id=str(run["account_scope"]),
    )


def _payload_matches_worker_run(payload: Dict[str, Any], strategy_run_id: str, refs: Optional[Dict[str, List[str]]] = None) -> bool:
    if _payload_matches_strategy_run(payload, strategy_run_id):
        return True
    if refs and _payload_matches_live_attribution(payload, refs):
        return True
    return False


async def _websocket_is_disconnected(websocket: WebSocket) -> bool:
    state = getattr(websocket, "client_state", None)
    if state is not None and str(getattr(state, "name", state)).upper() == "DISCONNECTED":
        return True
    return False


async def _worker_run_pnl_stream_ws(websocket: WebSocket, run: Dict[str, Any], *, interval_seconds: float) -> AsyncGenerator[tuple[str, Dict[str, Any]], None]:
    strategy_run_id = str(run["strategy_run_id"])
    current_run = dict(run)
    last_signature: Optional[str] = None
    heartbeat_counter = 0
    safe_interval = min(5.0, max(0.25, float(interval_seconds or 1.0)))
    while True:
        if await _websocket_is_disconnected(websocket):
            break
        refreshed_run = await _repo(websocket).get_run(strategy_run_id)
        if refreshed_run is None:
            yield "end", {"detail": "Strategy run not found"}
            break
        current_run = refreshed_run
        try:
            snapshot = await _build_worker_run_pnl_snapshot(websocket, current_run)
        except Exception as exc:
            yield "error", {"detail": str(exc)}
            await asyncio.sleep(safe_interval)
            continue
        signature = _snapshot_signature(snapshot)
        if signature != last_signature:
            yield "snapshot", snapshot
            last_signature = signature
            heartbeat_counter = 0
        else:
            heartbeat_counter += 1
            if heartbeat_counter >= max(1, int(15 / safe_interval)):
                yield "heartbeat", {"strategy_run_id": strategy_run_id}
                heartbeat_counter = 0
        await asyncio.sleep(safe_interval)


def _require_action(token: WorkerToken, action: str) -> None:
    if action not in set(token.allowed_actions):
        raise HTTPException(status_code=403, detail=f"Worker token is not allowed to perform '{action}'")


def _require_v1_mode(mode: str) -> None:
    normalized = str(mode or "").strip().lower()
    if normalized not in ALLOWED_V1_MODES:
        raise HTTPException(status_code=403, detail="Algo worker API allows only paper, dry_run, and explicitly enabled live execution modes")


def _broker_user_id_from_account_scope(account_scope: str) -> str:
    try:
        parsed = parse_account_scope(account_scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Live worker execution requires a kite:<broker_user_id> account_scope") from exc
    if parsed.mode != "live" or not parsed.broker_user_id:
        raise HTTPException(status_code=400, detail="Live worker execution requires a real broker account_scope, not a paper account scope")
    return parsed.broker_user_id


def _validate_live_run_contract(*, account_scope: str, metadata: Dict[str, Any]) -> None:
    _broker_user_id_from_account_scope(account_scope)
    missing = sorted(key for key in LIVE_REQUIRED_RUN_METADATA if not str(metadata.get(key) or "").strip())
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Live worker runs require metadata fields: {', '.join(missing)}",
        )
    strategy_family = str(metadata.get("strategy_family") or "").strip()
    if strategy_family not in VALID_WORKER_STRATEGY_FAMILIES:
        raise HTTPException(
            status_code=400,
            detail=f"Live worker metadata.strategy_family must be one of: {', '.join(sorted(VALID_WORKER_STRATEGY_FAMILIES))}",
        )


def _normalized_backend_protection_runtime_state(payload: Any, *, live: bool) -> Dict[str, Any]:
    return validate_backend_protection_payload(payload, live=live).to_runtime_state()


def _initial_backend_protection_state(protection: Dict[str, Any], *, generation: int = 1, reason: Optional[str] = None) -> Dict[str, Any]:
    return {
        "status": "active" if protection.get("enabled") else "disabled",
        "generation": generation,
        "version": int(protection.get("version") or 1),
        "update_reason": reason,
        "updated_at": _utcnow().isoformat(),
        "last_checked_at": None,
        "triggered_rule": None,
        "action": None,
        "exit_submitted": False,
        "errors": [],
    }


def _next_backend_protection_for_patch(protection: Dict[str, Any], previous_runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    previous_config = dict(previous_runtime_state.get("backend_protection") or {})
    previous_version = _to_int(previous_config.get("version"), default=0)
    next_protection = dict(protection)
    next_protection["version"] = max(_to_int(next_protection.get("version"), default=1), previous_version + 1)
    return next_protection


def _preserve_backend_trailing_state(next_state: Dict[str, Any], previous_state: Dict[str, Any]) -> Dict[str, Any]:
    preserved = dict(next_state)
    if "best_basket_pnl_pct" in previous_state:
        preserved["best_basket_pnl_pct"] = previous_state.get("best_basket_pnl_pct")
    previous_positions = previous_state.get("position_states")
    if isinstance(previous_positions, dict):
        preserved["position_states"] = dict(previous_positions)
    return preserved


def _load_live_kite_for_account(account_scope: str):
    broker_user_id = _broker_user_id_from_account_scope(account_scope)
    from broker_api.kite_session import build_kite_client

    db = SessionLocal()
    try:
        row = db.execute(
            text(
                """
                SELECT session_id, access_token
                FROM public.kite_sessions
                WHERE broker_user_id = :broker_user_id
                  AND access_token IS NOT NULL
                ORDER BY CASE WHEN session_id = 'system' THEN 0 ELSE 1 END, created_at DESC
                LIMIT 1
                """
            ),
            {"broker_user_id": broker_user_id},
        ).fetchone()
        if not row:
            raise HTTPException(status_code=503, detail="No live Kite broker session is available for this worker account_scope")
        return build_kite_client(str(row[1]), session_id=str(row[0]))
    finally:
        db.close()


def _live_attribution_for_worker_intent(*, token: WorkerToken, run: Dict[str, Any], request: WorkerIntentRequest) -> Dict[str, Any]:
    metadata = dict(run.get("metadata") or {})
    account_scope = str(run.get("account_scope") or token.account_scope or "")
    _validate_live_run_contract(account_scope=account_scope, metadata=metadata)
    return {
        "strategy_run_id": str(run["strategy_run_id"]),
        "strategy_family": str(metadata["strategy_family"]),
        "strategy_name": str(metadata["strategy_name"]),
        "execution_mode": "live",
        "account_ref": account_scope,
        "entry_surface": str(metadata.get("entry_surface") or "algo_worker"),
        "journal_run_id": metadata.get("journal_run_id") or None,
        "source": "algo_worker",
        "idempotency_key": request.idempotency_key,
        "metadata": {
            "token_id": token.token_id,
            "template_id": run.get("template_id"),
            "worker_run_metadata": metadata,
            "intent_metadata": request.metadata,
        },
    }


def _paper_attribution_for_worker_intent(*, token: WorkerToken, run: Dict[str, Any], request: WorkerIntentRequest) -> Dict[str, Any]:
    metadata = dict(run.get("metadata") or {})
    account_scope = str(run.get("account_scope") or token.account_scope or "")
    strategy_family = str(metadata.get("strategy_family") or "indicator_strategy").strip()
    if strategy_family not in VALID_WORKER_STRATEGY_FAMILIES:
        strategy_family = "indicator_strategy"
    strategy_name = str(metadata.get("strategy_name") or run.get("template_id") or run.get("strategy_run_id") or "paper-run").strip()
    return build_paper_execution_attribution(
        strategy_run_id=str(run["strategy_run_id"]),
        strategy_family=strategy_family,
        strategy_name=strategy_name,
        account_ref=account_scope,
        entry_surface=str(metadata.get("entry_surface") or "algo_worker"),
        source="algo_worker",
        idempotency_key=request.idempotency_key,
        metadata=request.metadata,
        extras={
            "token_id": token.token_id,
            "template_id": run.get("template_id"),
            "strategy_id": str(run["strategy_run_id"]),
            "option_strategy_id": str(run["strategy_run_id"]),
            "strategy_tag": metadata.get("strategy_tag") or run.get("template_id"),
            "algo_instance_id": metadata.get("algo_instance_id"),
            "journal_run_id": metadata.get("journal_run_id") or None,
            "journal_ref": metadata.get("journal_ref") or None,
            "worker_run_metadata": metadata,
            "intent_metadata": request.metadata,
        },
    )


def _inject_live_attribution(order_payload: Dict[str, Any], attribution: Dict[str, Any]) -> Dict[str, Any]:
    order = dict(order_payload)
    order["attribution"] = dict(attribution)
    return order


def _worker_pnl_side(net_quantity: int) -> str:
    if net_quantity > 0:
        return "LONG"
    if net_quantity < 0:
        return "SHORT"
    return "FLAT"


def _empty_worker_pnl_snapshot(run: Dict[str, Any], *, is_realtime: bool, is_stale: bool = False, updated_at: Optional[str] = None) -> Dict[str, Any]:
    return WorkerRunPnlSnapshot(
        strategy_run_id=str(run["strategy_run_id"]),
        execution_mode=str(run.get("execution_mode") or "dry_run"),
        status=str(run.get("status") or "open"),
        currency="INR",
        totals=WorkerRunPnlTotals(),
        legs=[],
        position_count=0,
        is_realtime=is_realtime,
        is_stale=is_stale,
        updated_at=updated_at or _run_updated_at(run) or "1970-01-01T00:00:00+00:00",
    ).model_dump(mode="json")


def _snapshot_signature(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=_json_default, separators=(",", ":"))


def _numeric_dict_total(payload: Any, *, exclude: Optional[set[str]] = None) -> float:
    if not isinstance(payload, dict):
        return 0.0
    excluded = exclude or set()
    return sum(_to_float(value) for key, value in payload.items() if key not in excluded and isinstance(value, (int, float, str)))


def _worker_margin_segment(payload: Any) -> WorkerFundsSegment:
    segment = dict(payload or {}) if isinstance(payload, dict) else {}
    available = dict(segment.get("available") or {}) if isinstance(segment.get("available"), dict) else {}
    utilised = dict(segment.get("utilised") or {}) if isinstance(segment.get("utilised"), dict) else {}
    available_cash = _to_float(available.get("cash"), default=_to_float(segment.get("net")))
    return WorkerFundsSegment(
        net=_to_float(segment.get("net")),
        available_cash=available_cash,
        opening_balance=_to_float(available.get("opening_balance")),
        live_balance=_to_float(available.get("live_balance")) if "live_balance" in available else None,
        collateral=_to_float(available.get("collateral")) if "collateral" in available else None,
        utilised=_numeric_dict_total(utilised, exclude={"m2m_realised", "m2m_unrealised"}),
        m2m_realised=_to_float(utilised.get("m2m_realised")),
        m2m_unrealised=_to_float(utilised.get("m2m_unrealised")),
    )


async def _paper_worker_funds_snapshot(request: Request, account_scope: str, *, mode: str) -> Dict[str, Any]:
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    account = await paper_runtime_service.get_account_summary(account_scope)
    available_funds = _to_float(account.get("available_funds"))
    blocked_funds = _to_float(account.get("blocked_funds"))
    realized_pnl = _to_float(account.get("realized_pnl"))
    segment = WorkerFundsSegment(
        net=available_funds + blocked_funds,
        available_cash=available_funds,
        opening_balance=_to_float(account.get("starting_balance")),
        utilised=blocked_funds,
        m2m_realised=realized_pnl,
        m2m_unrealised=0.0,
    )
    return WorkerFundsSnapshot(
        account_scope=account_scope,
        mode=mode,
        currency=str(account.get("currency") or "INR"),
        source="paper_runtime",
        segments={"equity": segment},
        allocation={"usable_equity_cash": available_funds, "max_new_position_value": available_funds},
        stale=False,
        updated_at=str(account.get("updated_at") or _utcnow().isoformat()),
    ).model_dump(mode="json")


async def _live_worker_funds_snapshot(account_scope: str, *, mode: str) -> Dict[str, Any]:
    kite = _load_live_kite_for_account(account_scope)
    margins = await asyncio.to_thread(kite.margins)
    segments = {
        name: _worker_margin_segment(payload)
        for name, payload in dict(margins or {}).items()
        if name in {"equity", "commodity"}
    }
    equity = segments.get("equity") or WorkerFundsSegment()
    return WorkerFundsSnapshot(
        account_scope=account_scope,
        mode=mode,
        currency="INR",
        source="broker",
        segments=segments,
        allocation={"usable_equity_cash": equity.available_cash, "max_new_position_value": equity.available_cash},
        stale=False,
        updated_at=_utcnow().isoformat(),
    ).model_dump(mode="json")


async def _build_worker_funds_snapshot(request: Request, *, account_scope: str, mode: str) -> Dict[str, Any]:
    normalized_mode = str(mode or "paper").lower()
    try:
        parsed_scope = parse_account_scope(account_scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if normalized_mode == "paper":
        return await _paper_worker_funds_snapshot(request, account_scope, mode=normalized_mode)
    if normalized_mode == "dry_run" and parsed_scope.mode == "paper":
        return await _paper_worker_funds_snapshot(request, account_scope, mode=normalized_mode)
    if normalized_mode in {"live", "dry_run"}:
        return await _live_worker_funds_snapshot(account_scope, mode=normalized_mode)
    raise HTTPException(status_code=400, detail=f"Unsupported execution mode '{normalized_mode}'")


def _run_allocation_cap(run: Dict[str, Any]) -> Optional[float]:
    metadata = dict(run.get("metadata") or {})
    runtime_state = dict(run.get("runtime_state") or {})
    allocation_state = dict(runtime_state.get("allocation") or {}) if isinstance(runtime_state.get("allocation"), dict) else {}
    for value in (
        metadata.get("allocation_cap"),
        metadata.get("allocation_cap_inr"),
        allocation_state.get("cap"),
        runtime_state.get("allocation_cap"),
    ):
        cap = _to_float(value, default=-1.0)
        if cap >= 0:
            return cap
    return None


def _run_usage_from_pnl(pnl: Dict[str, Any]) -> Dict[str, float]:
    gross_exposure = 0.0
    net_exposure = 0.0
    for leg in pnl.get("legs") or []:
        quantity = _to_int(leg.get("net_quantity"))
        mark = _to_float(leg.get("last_price"), default=0.0) or _to_float(leg.get("average_price"), default=0.0)
        gross_exposure += abs(quantity) * mark
        net_exposure += quantity * mark
    totals = dict(pnl.get("totals") or {})
    return {
        "gross_exposure": gross_exposure,
        "net_exposure": net_exposure,
        "realized_pnl": _to_float(totals.get("realized_pnl")),
        "unrealized_pnl": _to_float(totals.get("unrealized_pnl")),
        "net_pnl": _to_float(totals.get("net_pnl")),
    }


async def _build_worker_run_funds_snapshot(request: Request, run: Dict[str, Any]) -> Dict[str, Any]:
    account_funds = await _build_worker_funds_snapshot(request, account_scope=str(run["account_scope"]), mode=str(run.get("execution_mode") or "paper"))
    pnl = await _build_worker_run_pnl_snapshot(request, run)
    usage = _run_usage_from_pnl(pnl)
    cap = _run_allocation_cap(run)
    used = usage["gross_exposure"]
    allocation = {
        "cap": cap,
        "used": used,
        "remaining": max(0.0, cap - used) if cap is not None else None,
        "basis": "gross_exposure",
    }
    return {
        **account_funds,
        "strategy_run_id": str(run["strategy_run_id"]),
        "strategy": {
            "strategy_run_id": str(run["strategy_run_id"]),
            "status": str(run.get("status") or "open"),
            **usage,
            "estimated_margin_used": None,
            "allocation": allocation,
            "pnl": pnl.get("totals") or {},
            "position_count": _to_int(pnl.get("position_count")),
            "is_stale": bool(pnl.get("is_stale")),
        },
        "allocation": {**dict(account_funds.get("allocation") or {}), "run": allocation},
    }


def _run_updated_at(run: Dict[str, Any]) -> Optional[str]:
    runtime_state = dict(run.get("runtime_state") or {})
    for key in ("updated_at", "created_at"):
        value = run.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            return value
    for key in ("live_exit_finalized_at", "updated_at"):
        value = runtime_state.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            return value
    return None


def _accumulate_leg_fact(state: Dict[str, Any], *, side: str, quantity: int, price: float, charges: float) -> None:
    net_quantity = _to_int(state.get("net_quantity"))
    average_price = _to_float(state.get("average_price"))
    signed_quantity = quantity if side == "BUY" else -quantity

    state["charges"] = _to_float(state.get("charges")) + charges

    if net_quantity == 0 or (net_quantity > 0 and signed_quantity > 0) or (net_quantity < 0 and signed_quantity < 0):
        existing_abs = abs(net_quantity)
        incoming_abs = abs(signed_quantity)
        combined = existing_abs + incoming_abs
        state["average_price"] = price if combined == 0 else ((average_price * existing_abs) + (price * incoming_abs)) / combined
        state["net_quantity"] = net_quantity + signed_quantity
        return

    closing_quantity = min(abs(net_quantity), abs(signed_quantity))
    realized_pnl = _to_float(state.get("realized_pnl"))
    if net_quantity > 0 and signed_quantity < 0:
        realized_pnl += (price - average_price) * closing_quantity
    elif net_quantity < 0 and signed_quantity > 0:
        realized_pnl += (average_price - price) * closing_quantity
    state["realized_pnl"] = realized_pnl

    remaining_existing = abs(net_quantity) - closing_quantity
    remaining_incoming = abs(signed_quantity) - closing_quantity
    if remaining_existing > 0:
        state["net_quantity"] = remaining_existing if net_quantity > 0 else -remaining_existing
        state["average_price"] = average_price
        return
    if remaining_incoming > 0:
        state["net_quantity"] = remaining_incoming if signed_quantity > 0 else -remaining_incoming
        state["average_price"] = price
        return
    state["net_quantity"] = 0
    state["average_price"] = 0.0


def _build_live_worker_leg_states(facts: List[Any]) -> Tuple[Dict[Tuple[int, str], Dict[str, Any]], float]:
    legs: Dict[Tuple[int, str], Dict[str, Any]] = {}
    total_charges = 0.0
    ordered_facts = sorted(facts, key=lambda item: (getattr(item, "fill_timestamp", _utcnow()), getattr(item, "id", 0) or 0))
    for fact in ordered_facts:
        payload = dict(getattr(fact, "payload", {}) or {})
        broker_fill = dict(payload.get("broker_fill") or {})
        instrument_token = _to_int(broker_fill.get("instrument_token"))
        product = str(broker_fill.get("product") or "")
        if not instrument_token or not product:
            continue
        key = (instrument_token, product)
        state = legs.setdefault(
            key,
            {
                "instrument_token": instrument_token,
                "exchange": str(broker_fill.get("exchange") or "") or None,
                "tradingsymbol": str(broker_fill.get("tradingsymbol") or "") or None,
                "product": product,
                "net_quantity": 0,
                "average_price": 0.0,
                "realized_pnl": 0.0,
                "charges": 0.0,
                "last_fill_at": getattr(fact, "fill_timestamp", None),
            },
        )
        side = str(getattr(fact, "side", "") or "").upper()
        quantity = _to_int(getattr(fact, "quantity", 0))
        price = _to_float(getattr(fact, "price", 0.0))
        fact_charges = _to_float(getattr(fact, "fees_amount", 0.0)) + _to_float(getattr(fact, "taxes_amount", 0.0)) + _to_float(getattr(fact, "slippage_amount", 0.0))
        total_charges += fact_charges
        _accumulate_leg_fact(state, side=side, quantity=quantity, price=price, charges=fact_charges)
        state["last_fill_at"] = getattr(fact, "fill_timestamp", None) or state.get("last_fill_at")
    return legs, total_charges


async def _paper_worker_run_pnl_snapshot(request: Request, run: Dict[str, Any]) -> Dict[str, Any]:
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    summary = await paper_runtime_service.get_strategy_run_pnl(str(run["account_scope"]), str(run["strategy_run_id"]))
    if summary is None:
        return _empty_worker_pnl_snapshot(run, is_realtime=True)

    strategy = dict(summary.get("strategy") or {})
    legs = [
        WorkerRunPnlLeg(
            instrument_token=position.get("instrument_token"),
            exchange=position.get("exchange"),
            tradingsymbol=position.get("tradingsymbol"),
            product=position.get("product"),
            net_quantity=_to_int(position.get("net_quantity")),
            side=str(position.get("side") or _worker_pnl_side(_to_int(position.get("net_quantity")))),
            average_price=_to_float(position.get("average_price")),
            last_price=_to_float(position.get("last_price")),
            realized_pnl=_to_float(position.get("realized_pnl")),
            unrealized_pnl=_to_float(position.get("unrealized_pnl")),
            gross_pnl=_to_float(position.get("gross_pnl"))
            if "gross_pnl" in position
            else (_to_float(position.get("realized_pnl")) + _to_float(position.get("unrealized_pnl"))),
            charges=_to_float(position.get("charges")),
            net_pnl=_to_float(position.get("net_pnl"))
            if "net_pnl" in position
            else (
                (_to_float(position.get("gross_pnl")) if "gross_pnl" in position else (_to_float(position.get("realized_pnl")) + _to_float(position.get("unrealized_pnl"))))
                - _to_float(position.get("charges"))
            ),
            is_stale=bool(position.get("is_stale")),
        )
        for position in strategy.get("positions", [])
        if _to_int(position.get("net_quantity")) != 0
    ]
    realized = _to_float(strategy.get("realized_pnl"))
    unrealized = _to_float(strategy.get("unrealized_pnl"))
    gross = _to_float(strategy.get("gross_pnl")) if "gross_pnl" in strategy else (realized + unrealized)
    charges = _to_float(strategy.get("charges"))
    if charges == 0.0:
        charges = sum(_to_float(getattr(leg, "charges", 0.0)) for leg in legs)
    net = _to_float(strategy.get("net_pnl")) if "net_pnl" in strategy else (gross - charges)
    updated_at = str(strategy.get("last_updated_at") or strategy.get("last_event_at") or _utcnow().isoformat())
    return WorkerRunPnlSnapshot(
        strategy_run_id=str(run["strategy_run_id"]),
        execution_mode="paper",
        status=str(strategy.get("status") or run.get("status") or "open"),
        currency=str(summary.get("currency") or "INR"),
        totals=WorkerRunPnlTotals(realized_pnl=realized, unrealized_pnl=unrealized, gross_pnl=gross, charges=charges, net_pnl=net),
        legs=legs,
        position_count=len(legs),
        is_realtime=True,
        is_stale=bool(strategy.get("is_stale")),
        updated_at=updated_at,
    ).model_dump(mode="json")


async def _live_worker_run_pnl_snapshot(request: Request, run: Dict[str, Any]) -> Dict[str, Any]:
    strategy_run_id = str(run["strategy_run_id"])
    account_id = str(run["account_scope"])

    realtime_positions = getattr(request.app.state, "algo_worker_realtime_positions_service", None)
    if realtime_positions is None:
        from broker_api.order_runtime import realtime_positions_service as realtime_positions

    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-pnl-{uuid.uuid4()}"
    positions = await realtime_positions.get_positions(account_id, corr_id)
    positions_by_leg: Dict[Tuple[int, str], Any] = {}
    for position in positions.values():
        positions_by_leg[(int(position.instrument_token), str(position.product))] = position

    from journaling.repository import JournalRepository

    journal_repository = getattr(request.app.state, "algo_worker_journal_repository", None) or JournalRepository()
    link = await asyncio.to_thread(journal_repository.find_source_link, source_type="live_order", source_key=strategy_run_id)
    live_facts: List[Any] = []
    if link is not None:
        facts = await asyncio.to_thread(journal_repository.list_execution_facts, str(link.run_id))
        live_facts = [fact for fact in facts if str(getattr(fact, "source_type", "")) == "live_fill"]

    if live_facts:
        legs_by_key, total_charges = _build_live_worker_leg_states(live_facts)
    else:
        attributed_legs = await _repo(request).list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)
        legs_by_key = {}
        total_charges = 0.0
        for leg in attributed_legs:
            instrument_token = _to_int(leg.get("instrument_token"))
            product = str(leg.get("product") or "")
            if not instrument_token or not product:
                continue
            net_quantity = _to_int(leg.get("net_quantity"))
            position = positions_by_leg.get((instrument_token, product))
            average_price = _to_float(getattr(position, "average_price", 0.0)) if position is not None else 0.0
            legs_by_key[(instrument_token, product)] = {
                "instrument_token": instrument_token,
                "exchange": leg.get("exchange"),
                "tradingsymbol": leg.get("tradingsymbol"),
                "product": product,
                "net_quantity": net_quantity,
                "average_price": average_price,
                "realized_pnl": 0.0,
                "charges": 0.0,
                "last_fill_at": None,
            }

    rendered_legs: List[WorkerRunPnlLeg] = []
    unrealized_total = 0.0
    realized_total = 0.0
    updated_markers: List[str] = []
    stale = False

    for key, state in sorted(legs_by_key.items(), key=lambda item: ((item[1].get("exchange") or ""), (item[1].get("tradingsymbol") or ""), (item[1].get("product") or ""))):
        position = positions_by_leg.get(key)
        net_quantity = _to_int(state.get("net_quantity"))
        if position is not None:
            last_price = _to_float(getattr(position, "last_price", 0.0))
            broker_net_quantity = _to_int(getattr(position, "quantity", 0))
            last_reconciled_at = getattr(position, "last_reconciled_at", None)
            if last_reconciled_at:
                updated_markers.append(str(last_reconciled_at))
        else:
            last_price = 0.0
            broker_net_quantity = None
            last_reconciled_at = None

        realized = _to_float(state.get("realized_pnl"))
        charges = _to_float(state.get("charges"))
        average_price = _to_float(state.get("average_price"))
        unrealized = 0.0
        leg_stale = False
        if net_quantity != 0:
            if last_price > 0:
                unrealized = (last_price - average_price) * net_quantity
            else:
                leg_stale = True
            if broker_net_quantity is None:
                leg_stale = True
            elif broker_net_quantity != net_quantity:
                leg_stale = True

        stale = stale or leg_stale
        realized_total += realized
        unrealized_total += unrealized
        gross = realized + unrealized
        net = gross - charges
        rendered_legs.append(
            WorkerRunPnlLeg(
                instrument_token=state.get("instrument_token"),
                exchange=state.get("exchange"),
                tradingsymbol=state.get("tradingsymbol"),
                product=state.get("product"),
                net_quantity=net_quantity,
                side=_worker_pnl_side(net_quantity),
                average_price=average_price,
                last_price=last_price,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                gross_pnl=gross,
                charges=charges,
                net_pnl=net,
                broker_net_quantity=broker_net_quantity,
                is_stale=leg_stale,
                last_reconciled_at=str(last_reconciled_at) if last_reconciled_at else None,
            )
        )

    gross_total = realized_total + unrealized_total
    net_total = gross_total - total_charges
    if not updated_markers and live_facts:
        updated_markers = [getattr(live_facts[-1], "fill_timestamp", _utcnow()).isoformat()]
    updated_at = max(updated_markers) if updated_markers else (_run_updated_at(run) or _utcnow().isoformat())
    return WorkerRunPnlSnapshot(
        strategy_run_id=strategy_run_id,
        execution_mode="live",
        status=str(run.get("status") or "open"),
        currency="INR",
        totals=WorkerRunPnlTotals(
            realized_pnl=realized_total,
            unrealized_pnl=unrealized_total,
            gross_pnl=gross_total,
            charges=total_charges,
            net_pnl=net_total,
        ),
        legs=[leg for leg in rendered_legs if leg.net_quantity != 0],
        position_count=len([leg for leg in rendered_legs if leg.net_quantity != 0]),
        is_realtime=True,
        is_stale=stale,
        updated_at=updated_at,
    ).model_dump(mode="json")


async def _build_worker_run_pnl_snapshot(request: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(run.get("execution_mode") or "").lower()
    if mode == "dry_run":
        return _empty_worker_pnl_snapshot(run, is_realtime=False)
    if mode == "paper":
        return await _paper_worker_run_pnl_snapshot(request, run)
    if mode == "live":
        return await _live_worker_run_pnl_snapshot(request, run)
    raise HTTPException(status_code=400, detail=f"Unsupported execution mode '{mode}'")


async def _worker_run_pnl_stream(request: Request, run: Dict[str, Any], *, interval_seconds: float) -> AsyncGenerator[str, None]:
    strategy_run_id = str(run["strategy_run_id"])
    current_run = dict(run)
    last_signature: Optional[str] = None
    heartbeat_counter = 0
    safe_interval = min(5.0, max(0.25, float(interval_seconds or 1.0)))
    while True:
        if await request.is_disconnected():
            break
        refreshed_run = await _repo(request).get_run(strategy_run_id)
        if refreshed_run is None:
            yield "event: end\ndata: {\"detail\": \"Strategy run not found\"}\n\n"
            break
        current_run = refreshed_run
        try:
            snapshot = await _build_worker_run_pnl_snapshot(request, current_run)
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
            await asyncio.sleep(safe_interval)
            continue
        signature = _snapshot_signature(snapshot)
        if signature != last_signature:
            yield f"data: {json.dumps(snapshot, default=_json_default)}\n\n"
            last_signature = signature
            heartbeat_counter = 0
        else:
            heartbeat_counter += 1
            if heartbeat_counter >= max(1, int(15 / safe_interval)):
                yield ": heartbeat\n\n"
                heartbeat_counter = 0
        await asyncio.sleep(safe_interval)


async def _submit_live_worker_intent(*, request: Request, token: WorkerToken, run: Dict[str, Any], payload: WorkerIntentRequest) -> Dict[str, Any]:
    from broker_api.kite_orders import BasketOrderRequest, OrdersService, PlaceOrderRequest

    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-live-{uuid.uuid4()}"
    worker_session_id = f"worker:{token.token_id}:{run['strategy_run_id']}"
    attribution = _live_attribution_for_worker_intent(token=token, run=run, request=payload)

    if payload.intent_type == "place_order":
        order_payload = payload.payload.get("order") or payload.payload
        req = PlaceOrderRequest.model_validate(_inject_live_attribution(order_payload, attribution))
        result = await orders_service.place_order(
            kite,
            req,
            corr_id,
            idempotency_key=payload.idempotency_key,
            session_id=worker_session_id,
            response=Response(),
        )
        return {"mode": "live", "intent_type": payload.intent_type, "result": result.model_dump(mode="json")}

    if payload.intent_type == "place_basket":
        basket_payload = dict(payload.payload.get("basket") or payload.payload)
        orders = [_inject_live_attribution(order, attribution) for order in basket_payload.get("orders") or []]
        basket_payload["orders"] = orders
        req = BasketOrderRequest.model_validate(basket_payload)
        result = await orders_service.place_basket(
            kite,
            req,
            corr_id,
            session_id=worker_session_id,
            idempotency_key=payload.idempotency_key,
            response=Response(),
        )
        return {"mode": "live", "intent_type": payload.intent_type, "result": result.model_dump(mode="json")}

    raise HTTPException(status_code=400, detail=f"Unsupported intent_type '{payload.intent_type}'")


async def _refresh_live_account_state(*, kite: Any, account_id: str, corr_id: str) -> Dict[str, Any]:
    from broker_api.kite_orders import order_event_runtime, realtime_positions_service

    refresh_result: Dict[str, Any] = {"account_id": account_id}
    try:
        refresh_result["reconciled_positions"] = await realtime_positions_service.reconcile_account_positions(kite, account_id, corr_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to reconcile live broker positions before exit: {exc}") from exc

    try:
        refresh_result["synced_dirty_orders"] = await order_event_runtime.sync_dirty_orders(kite, realtime_positions_service, batch_size=25)
    except Exception as exc:
        refresh_result["sync_warning"] = str(exc)
    return refresh_result


def _validate_live_exit_legs(legs: List[Dict[str, Any]]) -> None:
    for leg in legs:
        net_quantity = int(leg.get("net_quantity") or 0)
        broker_net_quantity = leg.get("broker_net_quantity")
        if not leg.get("exchange") or not leg.get("tradingsymbol") or not leg.get("product") or not leg.get("instrument_token"):
            raise HTTPException(status_code=409, detail="Live exit cannot proceed because one or more attributed legs is missing broker instrument metadata")
        if broker_net_quantity is None:
            raise HTTPException(
                status_code=409,
                detail=f"Live exit cannot proceed because broker position is missing for {leg.get('exchange')}:{leg.get('tradingsymbol')} {leg.get('product')}",
            )
        broker_net = int(broker_net_quantity or 0)
        if net_quantity > 0 and broker_net < net_quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Live exit cannot proceed because broker net quantity for {leg.get('tradingsymbol')} is lower than the attributed long quantity",
            )
        if net_quantity < 0 and broker_net > net_quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Live exit cannot proceed because broker net quantity for {leg.get('tradingsymbol')} is lower than the attributed short quantity",
            )


def _live_exit_orders_from_legs(legs: List[Dict[str, Any]], attribution: Dict[str, Any]) -> List[Dict[str, Any]]:
    orders: List[Dict[str, Any]] = []
    for leg in legs:
        net_quantity = int(leg.get("net_quantity") or 0)
        if net_quantity == 0:
            continue
        orders.append(
            {
                "exchange": str(leg["exchange"]),
                "tradingsymbol": str(leg["tradingsymbol"]),
                "transaction_type": "SELL" if net_quantity > 0 else "BUY",
                "variety": "regular",
                "product": str(leg["product"]),
                "order_type": "MARKET",
                "quantity": abs(net_quantity),
                "validity": "DAY",
                "market_protection": -1,
                "attribution": dict(attribution),
            }
        )
    return orders


async def _live_broker_positions_for_attribution(
    request: Request,
    *,
    kite: Any,
    corr_id: str,
    refs: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    from broker_api.kite_orders import OrdersService

    if not (refs.get("broker_order_ids") or refs.get("client_order_refs")):
        return []

    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    broker_orders = await asyncio.to_thread(orders_service.orders, kite, corr_id)
    matched_orders = [
        _serialize_model(order)
        for order in broker_orders
        if _payload_matches_live_attribution(_serialize_model(order), refs)
    ]
    if not matched_orders:
        return []

    positions_payload = await asyncio.to_thread(kite.positions)
    net_positions = positions_payload.get("net", []) if isinstance(positions_payload, dict) else []
    positions_by_key: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
    for position in net_positions:
        instrument_token = _to_int(position.get("instrument_token"))
        product = str(position.get("product") or "")
        exchange = str(position.get("exchange") or "")
        tradingsymbol = str(position.get("tradingsymbol") or "")
        if not instrument_token or not product or not exchange or not tradingsymbol:
            continue
        positions_by_key[(instrument_token, product, exchange, tradingsymbol)] = dict(position)

    exposure: List[Dict[str, Any]] = []
    seen: set[Tuple[int, str, str, str]] = set()
    for order in matched_orders:
        key = (
            _to_int(order.get("instrument_token")),
            str(order.get("product") or ""),
            str(order.get("exchange") or ""),
            str(order.get("tradingsymbol") or ""),
        )
        if key in seen or not all(key):
            continue
        seen.add(key)
        position = positions_by_key.get(key)
        if not position:
            continue
        if _to_int(position.get("quantity") or position.get("net_quantity")) == 0:
            continue
        exposure.append(
            {
                "account_id": position.get("account_id") or position.get("account_ref"),
                "instrument_token": key[0],
                "product": key[1],
                "exchange": key[2],
                "tradingsymbol": key[3],
                "net_quantity": _to_int(position.get("quantity") or position.get("net_quantity")),
                "average_price": _to_float(position.get("average_price")),
                "last_price": _to_float(position.get("last_price")),
            }
        )
    return exposure


def _live_exit_idempotency_key(*, strategy_run_id: str, legs: List[Dict[str, Any]], supplied_key: Optional[str]) -> str:
    if supplied_key:
        return supplied_key
    normalized = [
        {
            "instrument_token": int(leg.get("instrument_token") or 0),
            "product": str(leg.get("product") or ""),
            "net_quantity": int(leg.get("net_quantity") or 0),
        }
        for leg in legs
    ]
    digest = hashlib.sha1(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
    run_digest = hashlib.sha1(strategy_run_id.encode("utf-8")).hexdigest()[:8]
    return f"live-exit:{run_digest}:{digest}"


async def _exit_live_worker_run(*, request: Request, token: WorkerToken, run: Dict[str, Any], payload: WorkerExitRequest) -> Dict[str, Any]:
    from broker_api.kite_orders import BasketOrderRequest, OrdersService

    strategy_run_id = str(run["strategy_run_id"])
    if str(run.get("status") or "") == "closed":
        return {"mode": "live", "status": "closed", "message": "Live worker run is already closed", "run": run}

    account_id = str(run["account_scope"])
    kite = await asyncio.to_thread(_load_live_kite_for_account, account_id)
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-live-exit-{uuid.uuid4()}"
    refresh_result = await _refresh_live_account_state(kite=kite, account_id=account_id, corr_id=corr_id)
    legs = await _repo(request).list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)

    if not legs:
        attribution_refs = await _worker_run_live_attribution_refs(request, run)
        broker_positions = await _repo(request).list_live_strategy_broker_positions(
            strategy_run_id=strategy_run_id,
            account_id=account_id,
        )
        if not broker_positions:
            broker_positions = await _live_broker_positions_for_attribution(
                request,
                kite=kite,
                corr_id=corr_id,
                refs=attribution_refs,
            )
        if broker_positions:
            return {
                "mode": "live",
                "status": "deferred",
                "deferred": True,
                "message": "Live exit attribution is still synchronizing; broker exposure exists so the run cannot be marked flat yet",
                "broker_positions": broker_positions,
                "refresh": refresh_result,
                "run": run,
            }
        updated = await _repo(request).update_run_status(
            strategy_run_id,
            "closed",
            state_patch={
                "exit_reason": payload.reason or "live_worker_flat",
                "live_exit_finalized_at": _utcnow().isoformat(),
                "live_exit_flat_confirmation": {"source": "live_order_attribution", "refresh": refresh_result},
            },
        )
        return {"mode": "live", "status": "closed", "message": "Live worker run is already flat", "run": updated}

    _validate_live_exit_legs(legs)
    exit_idempotency_key = _live_exit_idempotency_key(
        strategy_run_id=strategy_run_id,
        legs=legs,
        supplied_key=payload.idempotency_key,
    )
    live_exit_state = dict((run.get("runtime_state") or {}).get("live_exit") or {})
    if live_exit_state.get("idempotency_key") == exit_idempotency_key and live_exit_state.get("order_result"):
        return {
            "mode": "live",
            "status": str(run.get("status") or "exiting"),
            "message": "Live exit was already submitted for this position state",
            "run": run,
            "exit": live_exit_state,
        }

    attribution = _live_attribution_for_worker_intent(
        token=token,
        run=run,
        request=WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key=exit_idempotency_key,
            payload={},
            metadata={"exit_reason": payload.reason or "live_worker_exit"},
        ),
    )
    orders = _live_exit_orders_from_legs(legs, attribution)
    planned_exit = {
        "idempotency_key": exit_idempotency_key,
        "reason": payload.reason or "live_worker_exit",
        "dry_run": payload.dry_run,
        "planned_at": _utcnow().isoformat(),
        "legs": legs,
        "orders": orders,
        "refresh": refresh_result,
    }

    if payload.dry_run:
        return {"mode": "live", "status": "dry_run", "message": "Live exit dry run built without placing broker orders", "exit": planned_exit}

    await _repo(request).update_run_status(strategy_run_id, "exiting", state_patch={"live_exit": planned_exit, "exit_reason": payload.reason})
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    worker_session_id = f"worker:{token.token_id}:{strategy_run_id}:exit"
    req = BasketOrderRequest.model_validate({"orders": orders, "all_or_none": False, "dry_run": False})
    result = await orders_service.place_basket(
        kite,
        req,
        corr_id,
        session_id=worker_session_id,
        idempotency_key=exit_idempotency_key,
        response=Response(),
    )
    result_payload = result.model_dump(mode="json")
    planned_exit["submitted_at"] = _utcnow().isoformat()
    planned_exit["order_result"] = result_payload

    post_refresh = await _refresh_live_account_state(kite=kite, account_id=account_id, corr_id=corr_id)
    remaining_legs = await _repo(request).list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)
    planned_exit["post_submit_refresh"] = post_refresh
    planned_exit["remaining_legs"] = remaining_legs

    if not remaining_legs:
        updated = await _repo(request).update_run_status(
            strategy_run_id,
            "closed",
            state_patch={
                "live_exit": planned_exit,
                "exit_reason": payload.reason or "live_worker_exit",
                "live_exit_finalized_at": _utcnow().isoformat(),
                "live_exit_flat_confirmation": {"source": "live_order_attribution", "refresh": post_refresh},
            },
        )
        return {"mode": "live", "status": "closed", "result": result_payload, "run": updated}

    updated = await _repo(request).update_run_status(strategy_run_id, "exiting", state_patch={"live_exit": planned_exit, "exit_reason": payload.reason})
    return {
        "mode": "live",
        "status": "exiting",
        "message": "Live exit orders submitted; run remains open until broker fills confirm the strategy is flat",
        "result": result_payload,
        "remaining_legs": remaining_legs,
        "run": updated,
    }


def _assert_run_access(token: WorkerToken, run: Dict[str, Any]) -> None:
    if str(run.get("token_id") or "") != token.token_id:
        raise HTTPException(status_code=403, detail="Worker token cannot access this run")
    if not _token_allows_account_scope(token, str(run.get("account_scope") or "")):
        raise HTTPException(status_code=403, detail="Worker token cannot access this account scope")
    if token.allowed_templates and run.get("template_id") not in token.allowed_templates:
        raise HTTPException(status_code=403, detail="Worker token cannot access this strategy template")


def _token_allows_account_scope(token: WorkerToken, account_scope: str) -> bool:
    token_scope = str(token.account_scope or "").strip()
    if not token_scope:
        return True
    requested_scope = str(account_scope or "").strip()
    if not requested_scope:
        return False
    try:
        token_parsed = parse_account_scope(token_scope)
        requested_parsed = parse_account_scope(requested_scope)
    except ValueError:
        return token_scope == requested_scope

    if token_parsed.mode == "live":
        if requested_parsed.mode == "paper":
            return True
        return requested_parsed.normalized == token_parsed.normalized
    return requested_parsed.normalized == token_parsed.normalized


def _require_live_run(run: Dict[str, Any], *, feature: str) -> None:
    if str(run.get("execution_mode") or "").lower() != "live":
        raise HTTPException(status_code=409, detail=f"{feature} is only supported for live runs")


@router.post("/tokens", response_model=WorkerTokenCreateResponse)
async def create_worker_token(request: Request, payload: WorkerTokenCreateRequest):
    require_app_user(request)
    modes = {mode.lower() for mode in payload.allowed_modes}
    if not modes or not modes.issubset(ALLOWED_V1_MODES):
        raise HTTPException(status_code=400, detail="Worker tokens may only allow paper, dry_run, and live modes")
    if "live" in modes:
        _broker_user_id_from_account_scope(payload.account_scope or "")
    actions = set(payload.allowed_actions)
    if not actions or not actions.issubset(DEFAULT_WORKER_ACTIONS):
        raise HTTPException(status_code=400, detail="Worker token contains unsupported actions")

    raw_token = f"kwa_{secrets.token_urlsafe(32)}"
    token_id = f"worker_{uuid.uuid4().hex[:16]}"
    record = await _repo(request).create_token(payload, raw_token=raw_token, token_id=token_id)
    return WorkerTokenCreateResponse(token=raw_token, **record)


@router.get("/tokens", response_model=List[WorkerTokenView])
async def list_worker_tokens(request: Request):
    require_app_user(request)
    return await _repo(request).list_tokens()


@router.post("/tokens/{token_id}/revoke", response_model=WorkerTokenView)
async def revoke_worker_token(request: Request, token_id: str):
    require_app_user(request)
    record = await _repo(request).revoke_token(token_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Worker token not found")
    return record


@router.get("/worker/health")
async def worker_health(request: Request):
    token = await require_worker_token(request)
    return {
        "status": "ok",
        "token_id": token.token_id,
        "account_scope": token.account_scope,
        "allowed_modes": token.allowed_modes,
        "allowed_actions": token.allowed_actions,
        "allowed_templates": token.allowed_templates,
    }


@router.post("/worker/heartbeat")
async def worker_heartbeat(request: Request, payload: WorkerHeartbeatRequest):
    token = await require_worker_token(request)
    _require_action(token, "heartbeat")
    return await _repo(request).record_heartbeat(token.token_id, payload)


@router.post("/worker/runs")
async def create_worker_run(request: Request, payload: WorkerRunCreateRequest):
    token = await require_worker_token(request)
    _require_action(token, "runs:create")
    _require_v1_mode(payload.execution_mode)
    try:
        parsed_scope = parse_account_scope(payload.account_scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.execution_mode == "paper" and parsed_scope.mode != "paper":
        raise HTTPException(status_code=400, detail="Paper worker runs require a paper account_scope")
    if not _token_allows_account_scope(token, payload.account_scope):
        raise HTTPException(status_code=403, detail="Worker token cannot create runs for this account scope")
    if token.allowed_templates and payload.template_id not in token.allowed_templates:
        raise HTTPException(status_code=403, detail="Worker token cannot create this strategy template")
    if payload.execution_mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot use this execution mode")
    if payload.execution_mode == "live":
        _validate_live_run_contract(account_scope=payload.account_scope, metadata=payload.metadata)

    runtime_state = dict(payload.runtime_state or {})
    if "backend_protection" in runtime_state:
        try:
            runtime_state["backend_protection"] = _normalized_backend_protection_runtime_state(
                runtime_state.get("backend_protection"),
                live=payload.execution_mode == "live",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime_state["backend_protection_state"] = _initial_backend_protection_state(
            runtime_state["backend_protection"],
            generation=1,
            reason="run_create",
        )
        payload = payload.model_copy(update={"runtime_state": runtime_state})

    strategy_run_id = payload.strategy_run_id or f"run_{uuid.uuid4().hex}"

    metadata = dict(payload.metadata or {})
    runtime_state = dict(payload.runtime_state or {})
    worker_source_metadata = {
        "token_id": token.token_id,
        "worker_name": token.name,
        "allowed_templates": list(token.allowed_templates or []),
    }
    try:
        v2_refs = _journal_service(request).ensure_v2_worker_context(
            execution_mode=payload.execution_mode,
            account_scope=payload.account_scope,
            strategy_run_id=strategy_run_id,
            external_run_id=strategy_run_id,
            template_id=str(payload.template_id or "").strip() or None,
            worker_template_id=str(metadata.get("worker_template_id") or payload.template_id or "").strip() or None,
            strategy_name=str(metadata.get("strategy_name") or payload.template_id or strategy_run_id).strip(),
            strategy_family=str(metadata.get("strategy_family") or "indicator_strategy").strip(),
            scenario_key=str(metadata.get("scenario_key") or "").strip() or None,
            scenario_name=str(metadata.get("scenario_name") or "").strip() or None,
            deployment_key=str(metadata.get("deployment_key") or "").strip() or None,
            config_hash=str(metadata.get("config_hash") or "").strip() or None,
            source_system="algo_worker",
            entry_surface=str(metadata.get("entry_surface") or "algo_worker").strip() or "algo_worker",
            source_metadata=worker_source_metadata,
        )
        metadata["journal_v2"] = dict(v2_refs)
        runtime_state["journal_v2"] = {
            "environment_id": v2_refs.get("environment_id"),
            "execution_context_id": v2_refs.get("execution_context_id"),
            "template_id": v2_refs.get("template_id"),
            "variant_id": v2_refs.get("variant_id"),
            "deployment_id": v2_refs.get("deployment_id"),
        }
        payload = payload.model_copy(update={"metadata": metadata, "runtime_state": runtime_state})
    except Exception as exc:
        metadata.setdefault("journal_v2_warning", "context_resolution_failed")
        metadata.setdefault("journal_v2_warning_detail", str(exc))
        payload = payload.model_copy(update={"metadata": metadata, "runtime_state": runtime_state})
        logger.warning(
            "algo_worker_run_create_journal_v2_context_failed",
            extra={
                "strategy_run_id": strategy_run_id,
                "account_scope": payload.account_scope,
                "execution_mode": payload.execution_mode,
                "template_id": payload.template_id,
                "error": str(exc),
            },
        )

    return await _repo(request).create_run(token, payload, strategy_run_id=strategy_run_id)


@router.get("/worker/runs/{strategy_run_id}")
async def get_worker_run(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    return run


@router.get("/worker/market/instruments/resolve")
async def resolve_worker_market_ticker(request: Request, symbol: str):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).resolve_ticker(symbol)


@router.get("/worker/market/instruments/search")
async def search_worker_market_tickers(request: Request, query: str, exchange: Optional[str] = None, limit: int = Query(20, ge=1, le=50)):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).search_tickers(query, exchange=exchange, limit=limit)


@router.post("/worker/market/instruments/resolve")
async def resolve_worker_market_tickers(request: Request, payload: WorkerInstrumentResolveRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).resolve_many(symbols=payload.symbols, instrument_tokens=payload.instrument_tokens)


@router.post("/worker/market/quotes")
async def get_worker_market_quotes(request: Request, payload: WorkerQuoteRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_quotes(payload)


@router.get("/worker/market/ticks/stream")
async def stream_worker_market_ticks(request: Request, symbols: Optional[str] = None, tokens: Optional[str] = None, mode: str = "quote"):
    token = await require_worker_token(request)
    _require_action(token, "market:stream")
    parsed_symbols = _parse_csv_values(symbols)
    parsed_tokens = _parse_csv_int_values(tokens, field_name="tokens")
    return StreamingResponse(
        _market_data_service(request).stream_ticks(
            request,
            token,
            symbols=parsed_symbols,
            instrument_tokens=parsed_tokens,
            mode=mode,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/worker/market/candles")
async def get_worker_market_candles(
    request: Request,
    symbol: Optional[str] = None,
    instrument_token: Optional[int] = None,
    interval: str = "5minute",
    lookback: int = Query(50, ge=1, le=500),
):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_candles(
        symbol=symbol,
        instrument_token=instrument_token,
        interval=interval,
        lookback=lookback,
    )


@router.get("/worker/market/history")
async def get_worker_market_history(
    request: Request,
    background_tasks: BackgroundTasks,
    symbol: Optional[str] = None,
    instrument_token: Optional[int] = None,
    timeframe: str = "day",
    from_ts: Optional[datetime] = Query(None, alias="from"),
    to_ts: Optional[datetime] = Query(None, alias="to"),
    ingest: bool = True,
    passthrough: bool = False,
):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_historical_candles(
        symbol=symbol,
        instrument_token=instrument_token,
        timeframe=timeframe,
        from_date=from_ts,
        to_date=to_ts,
        ingest=ingest,
        passthrough=passthrough,
        background_tasks=background_tasks,
    )


@router.get("/worker/market/candles/stream")
async def stream_worker_market_candles(
    request: Request,
    symbol: Optional[str] = None,
    instrument_token: Optional[int] = None,
    interval: str = "5minute",
):
    token = await require_worker_token(request)
    _require_action(token, "market:stream")
    return StreamingResponse(
        _market_data_service(request).stream_candles(
            request,
            symbol=symbol,
            instrument_token=instrument_token,
            interval=interval,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/worker/market/snapshot")
async def get_worker_market_snapshot(request: Request, payload: WorkerMarketSnapshotRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_market_snapshot(payload)


@router.get("/worker/funds")
async def get_worker_funds(request: Request, mode: str = Query("paper"), account_scope: Optional[str] = None):
    token = await require_worker_token(request)
    _require_action(token, "funds:read")
    normalized_mode = str(mode or "paper").strip().lower()
    _require_v1_mode(normalized_mode)
    if normalized_mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot read funds for this execution mode")
    scope = str(account_scope or token.account_scope or "").strip()
    if not scope:
        raise HTTPException(status_code=400, detail="account_scope is required for worker funds")
    if not _token_allows_account_scope(token, scope):
        raise HTTPException(status_code=403, detail="Worker token cannot read this account scope")
    return await _build_worker_funds_snapshot(request, account_scope=scope, mode=normalized_mode)


@router.get("/worker/runs/{strategy_run_id}/funds")
async def get_worker_run_funds(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "funds:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    mode = str(run.get("execution_mode") or "paper").lower()
    if mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot read funds for this execution mode")
    return await _build_worker_run_funds_snapshot(request, run)


@router.get("/worker/runs/{strategy_run_id}/pnl")
async def get_worker_run_pnl(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    return await _build_worker_run_pnl_snapshot(request, run)


@router.get("/worker/runs/{strategy_run_id}/pnl/stream")
async def stream_worker_run_pnl(request: Request, strategy_run_id: str, interval_seconds: float = Query(1.0, ge=0.25, le=5.0)):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    return StreamingResponse(
        _worker_run_pnl_stream(request, run, interval_seconds=interval_seconds),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/worker/runs/{strategy_run_id}/risk")
async def patch_worker_run_risk(request: Request, strategy_run_id: str, payload: WorkerRiskPatchRequest):
    token = await require_worker_token(request)
    _require_action(token, "risk:update")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if run.get("status") in {"closed", "failed"}:
        raise HTTPException(status_code=409, detail="Closed strategy runs cannot be risk-edited")
    return await _repo(request).update_run_risk(strategy_run_id, payload.patch)


@router.patch("/worker/runs/{strategy_run_id}/protection")
async def patch_worker_run_protection(request: Request, strategy_run_id: str, payload: WorkerProtectionPatchRequest):
    token = await require_worker_token(request)
    _require_action(token, "risk:update")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if run.get("status") in {"closed", "failed"}:
        raise HTTPException(status_code=409, detail="Closed strategy runs cannot be protection-edited")

    runtime_state = dict(run.get("runtime_state") or {})
    previous_state = dict(runtime_state.get("backend_protection_state") or {})
    if previous_state.get("exit_submitted"):
        raise HTTPException(status_code=409, detail="Backend protection cannot be reset after a terminal protection exit")
    if previous_state.get("exit_claim_id"):
        raise HTTPException(status_code=409, detail="Backend protection exit is already in progress")

    try:
        protection = _normalized_backend_protection_runtime_state(
            payload.backend_protection,
            live=str(run.get("execution_mode") or "").lower() == "live",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    next_generation = _to_int(previous_state.get("generation"), default=0) + 1
    protection = _next_backend_protection_for_patch(protection, runtime_state)
    runtime_state["backend_protection"] = protection
    next_state = _initial_backend_protection_state(
        protection,
        generation=next_generation,
        reason=payload.reason,
    )
    if not payload.reset_trailing:
        next_state = _preserve_backend_trailing_state(next_state, previous_state)
    next_state["reset_trailing"] = payload.reset_trailing
    previous_generation = _to_int(previous_state.get("generation"), default=0)
    updated = await _repo(request).update_run_backend_protection(
        strategy_run_id,
        protection,
        next_state,
        expected_generation=previous_generation,
        expected_triggered_rule=previous_state.get("triggered_rule") or "",
        expected_exit_claim_id=previous_state.get("exit_claim_id") or "",
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Backend protection changed concurrently; reload and retry")
    return updated


@router.get("/worker/orders")
async def list_worker_orders(request: Request, strategy_run_id: str):
    from broker_api.kite_orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-orders-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    orders = await asyncio.to_thread(orders_service.orders, kite, corr_id)
    serialized = [_serialize_model(order) for order in orders]
    filtered = [order for order in serialized if _payload_matches_worker_run(order, strategy_run_id, attribution_refs)]
    return {"strategy_run_id": strategy_run_id, "orders": filtered}


@router.get("/worker/trades")
async def list_worker_trades(request: Request, strategy_run_id: str):
    from broker_api.kite_orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Trade inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-trades-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    trades = await asyncio.to_thread(orders_service.trades, kite, corr_id)
    serialized = [_serialize_model(trade) for trade in trades]
    filtered = [trade for trade in serialized if _payload_matches_worker_run(trade, strategy_run_id, attribution_refs)]
    return {"strategy_run_id": strategy_run_id, "trades": filtered}


@router.get("/worker/orders/{order_id}")
async def get_worker_order(request: Request, order_id: str, strategy_run_id: str):
    from broker_api.kite_orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-order-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    order = await asyncio.to_thread(orders_service.order_snapshot, kite, order_id, corr_id)
    payload = _serialize_model(order)
    if not _payload_matches_worker_run(payload, strategy_run_id, attribution_refs):
        raise HTTPException(status_code=404, detail="Order not found for strategy run")
    return {"strategy_run_id": strategy_run_id, "order": payload}


@router.get("/worker/orders/{order_id}/history")
async def get_worker_order_history(request: Request, order_id: str, strategy_run_id: str):
    from broker_api.kite_orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-order-history-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    history = await asyncio.to_thread(orders_service.order_history, kite, order_id, corr_id)
    entries = [_serialize_model(item) for item in history]
    if not entries or not any(_payload_matches_worker_run(item, strategy_run_id, attribution_refs) for item in entries):
        raise HTTPException(status_code=404, detail="Order not found for strategy run")
    return {"strategy_run_id": strategy_run_id, "order_id": order_id, "history": entries}


@router.post("/worker/orders/{order_id}/cancel")
async def cancel_worker_order(request: Request, order_id: str, payload: WorkerOrderActionRequest):
    from broker_api.kite_orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order cancellation")
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-cancel-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    result = await orders_service.cancel_order(
        kite,
        payload.variety or "regular",
        order_id,
        corr_id,
        parent_order_id=payload.parent_order_id,
    )
    return {"strategy_run_id": payload.strategy_run_id, "order_id": order_id, "result": _serialize_model(result)}


@router.post("/worker/orders/{order_id}/modify")
async def modify_worker_order(request: Request, order_id: str, payload: WorkerOrderModifyRequest):
    from broker_api.kite_orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order modification")
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-modify-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    result = await orders_service.modify_order(
        kite,
        payload.variety or "regular",
        order_id,
        payload.to_modify_request(),
        corr_id,
        parent_order_id=payload.parent_order_id,
    )
    return {"strategy_run_id": payload.strategy_run_id, "order_id": order_id, "result": _serialize_model(result)}


@router.post("/worker/runs/{strategy_run_id}/preview/order")
async def preview_worker_order(request: Request, strategy_run_id: str, payload: WorkerOrderPreviewRequest):
    from broker_api.kite_orders import OrdersService, PlaceOrderRequest
    from execution_accounting.kite_costs import build_live_order_cost_contract

    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if str(run.get("execution_mode") or "").lower() != "live":
        raise HTTPException(status_code=409, detail="Order preview is only required for live runs")

    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    order_payload = dict(payload.order or {})
    attribution = _live_attribution_for_worker_intent(
        token=token,
        run=run,
        request=WorkerIntentRequest(
            intent_type="place_order",
            idempotency_key=payload.idempotency_key or f"preview:{strategy_run_id}",
            payload={},
            metadata=payload.metadata or {},
        ),
    )
    req = PlaceOrderRequest.model_validate(_inject_live_attribution(order_payload, attribution))
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    cost_contract = build_live_order_cost_contract(
        kite=kite,
        orders_service=orders_service,
        order=req.model_dump(exclude_none=True),
        corr_id=f"preview-{strategy_run_id}",
    )
    return {
        "strategy_run_id": strategy_run_id,
        "mode": "live",
        "preview": {
            "intent_type": "place_order",
            "order": req.model_dump(mode="json", exclude_none=True),
            "cost_contract": cost_contract.journal_payload(),
        },
    }


@router.post("/worker/runs/{strategy_run_id}/preview/basket")
async def preview_worker_basket(request: Request, strategy_run_id: str, payload: WorkerBasketPreviewRequest):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if str(run.get("execution_mode") or "").lower() != "live":
        raise HTTPException(status_code=409, detail="Basket preview is only required for live runs")
    preview = await _submit_live_worker_intent(
        request=request,
        token=token,
        run=run,
        payload=WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key=payload.idempotency_key or f"preview:{strategy_run_id}:basket",
            payload={"basket": {"orders": payload.orders, "all_or_none": payload.all_or_none, "dry_run": True}},
            metadata=payload.metadata or {},
        ),
    )
    return {"strategy_run_id": strategy_run_id, "mode": "live", "preview": preview}


@router.post("/worker/runs/{strategy_run_id}/intents")
async def submit_worker_intent(request: Request, strategy_run_id: str, payload: WorkerIntentRequest):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if str(run.get("status") or "open") != "open":
        raise HTTPException(status_code=409, detail="Worker intents can only be submitted for open strategy runs")
    mode = str(run.get("execution_mode") or "").lower()
    _require_v1_mode(mode)
    if mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot submit intents for this execution mode")

    existing = await _repo(request).get_intent_result(strategy_run_id, payload.idempotency_key)
    if existing is not None:
        return {"status": "deduped", "result": existing}

    result: Dict[str, Any]
    if mode == "dry_run":
        attribution = build_execution_attribution(
            execution_mode="dry_run",
            strategy_run_id=str(run["strategy_run_id"]),
            strategy_family=str((run.get("metadata") or {}).get("strategy_family") or "indicator_strategy"),
            strategy_name=str((run.get("metadata") or {}).get("strategy_name") or run.get("template_id") or run["strategy_run_id"]),
            account_ref=str(run.get("account_scope") or ""),
            entry_surface=str((run.get("metadata") or {}).get("entry_surface") or "algo_worker"),
            source="algo_worker",
            idempotency_key=payload.idempotency_key,
            metadata=payload.metadata,
            extras={
                "token_id": token.token_id,
                "template_id": run.get("template_id"),
                "strategy_id": str(run["strategy_run_id"]),
                "option_strategy_id": str(run["strategy_run_id"]),
            },
        )
        result = {
            "mode": "dry_run",
            "status": "validated",
            "intent_type": payload.intent_type,
            "mutated_state": False,
            "payload": payload.payload,
            "attribution": attribution,
        }
    elif mode == "live":
        result = await _submit_live_worker_intent(request=request, token=token, run=run, payload=payload)
    else:
        paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
        if paper_runtime_service is None:
            raise HTTPException(status_code=503, detail="Paper runtime is not available")
        attribution = _paper_attribution_for_worker_intent(token=token, run=run, request=payload)
        if payload.intent_type == "place_order":
            result = await paper_runtime_service.place_order(
                account_scope=str(run["account_scope"]),
                order_payload=payload.payload.get("order") or payload.payload,
                attribution=attribution,
            )
        elif payload.intent_type == "place_basket":
            result = await paper_runtime_service.place_basket(
                account_scope=str(run["account_scope"]),
                basket_payload=payload.payload.get("basket") or payload.payload,
                attribution=attribution,
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported intent_type '{payload.intent_type}'")

    stored = await _repo(request).save_intent_result(
        token_id=token.token_id,
        strategy_run_id=strategy_run_id,
        request=payload,
        status=str(result.get("status") or "accepted"),
        result=result,
    )
    return {"status": "accepted", "result": stored}


@router.post("/worker/runs/{strategy_run_id}/exit")
async def exit_worker_run(request: Request, strategy_run_id: str, payload: WorkerExitRequest):
    token = await require_worker_token(request)
    _require_action(token, "runs:exit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    mode = str(run.get("execution_mode") or "").lower()
    _require_v1_mode(mode)
    if mode == "dry_run":
        updated = await _repo(request).update_run_status(strategy_run_id, "closed", state_patch={"exit_reason": payload.reason or "dry_run_exit"})
        return {"mode": "dry_run", "status": "closed", "run": updated}
    if mode == "live":
        return await _exit_live_worker_run(request=request, token=token, run=run, payload=payload)

    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    result = await paper_runtime_service.exit_strategy(account_scope=str(run["account_scope"]), strategy_id=strategy_run_id)
    result_status = str(result.get("status") or "success").lower()
    if result_status in {"success", "closed", "noop"}:
        updated = await _repo(request).update_run_status(strategy_run_id, "closed", state_patch={"exit_result": result, "exit_reason": payload.reason})
        return {"mode": "paper", "status": "closed", "result": result, "run": updated}
    updated = await _repo(request).update_run_status(strategy_run_id, str(run.get("status") or "open"), state_patch={"exit_result": result, "exit_reason": payload.reason})
    return {"mode": "paper", "status": result_status, "result": result, "run": updated}


@router.websocket("/worker/ws/market/ticks")
async def worker_ticks_ws(websocket: WebSocket):
    token = await require_worker_ws_token(websocket)
    _require_action(token, "market:stream")
    await websocket.accept()
    symbols = _parse_csv_values(websocket.query_params.get("symbols"))
    instrument_tokens = _parse_csv_int_values(websocket.query_params.get("tokens"), field_name="tokens")
    mode = websocket.query_params.get("mode") or "quote"
    async for event, payload in _market_data_service(websocket).stream_ticks_ws(
        websocket,
        token,
        symbols=symbols,
        instrument_tokens=instrument_tokens,
        mode=mode,
    ):
        await websocket.send_json({"event": event, "data": payload})


@router.websocket("/worker/ws/market/candles")
async def worker_candles_ws(websocket: WebSocket):
    token = await require_worker_ws_token(websocket)
    _require_action(token, "market:stream")
    await websocket.accept()
    instrument_token_param = websocket.query_params.get("instrument_token")
    instrument_token = normalize_instrument_token(instrument_token_param) if instrument_token_param else None
    async for event, payload in _market_data_service(websocket).stream_candles_ws(
        websocket,
        symbol=websocket.query_params.get("symbol"),
        instrument_token=instrument_token,
        interval=websocket.query_params.get("interval") or "5minute",
    ):
        await websocket.send_json({"event": event, "data": payload})


@router.websocket("/worker/ws/runs/{strategy_run_id}/pnl")
async def worker_run_pnl_ws(websocket: WebSocket, strategy_run_id: str):
    token = await require_worker_ws_token(websocket)
    _require_action(token, "runs:read")
    run = await _repo(websocket).get_run(strategy_run_id)
    if run is None:
        await websocket.close(code=4404)
        return
    _assert_run_access(token, run)
    try:
        interval_seconds = float(websocket.query_params.get("interval_seconds") or "1.0")
    except (TypeError, ValueError):
        await websocket.close(code=4400)
        return
    if interval_seconds < 0.25 or interval_seconds > 5.0:
        await websocket.close(code=4400)
        return
    await websocket.accept()
    async for event, payload in _worker_run_pnl_stream_ws(websocket, run, interval_seconds=interval_seconds):
        await websocket.send_json({"event": event, "data": payload})
