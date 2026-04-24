from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth_service import require_app_user
from database import SessionLocal


router = APIRouter(prefix="/algo-workers", tags=["Algo Workers"])

DEFAULT_WORKER_ACTIONS = {
    "runs:create",
    "runs:read",
    "intents:submit",
    "risk:update",
    "runs:exit",
    "heartbeat",
}
ALLOWED_V1_MODES = {"paper", "dry_run"}


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


class WorkerIntentRequest(BaseModel):
    intent_type: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=160)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkerExitRequest(BaseModel):
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None


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

    async def update_run_risk(self, strategy_run_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._update_run_risk_sync, strategy_run_id, patch)

    async def update_run_status(self, strategy_run_id: str, status: str, *, state_patch: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._update_run_status_sync, strategy_run_id, status, state_patch)

    async def get_intent_result(self, strategy_run_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_intent_result_sync, strategy_run_id, idempotency_key)

    async def save_intent_result(self, *, token_id: str, strategy_run_id: str, request: WorkerIntentRequest, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._save_intent_result_sync, token_id, strategy_run_id, request, status, result)

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


def _repo(request: Request) -> Any:
    repository = getattr(request.app.state, "algo_worker_repository", None)
    if repository is None:
        repository = SqlAlchemyAlgoWorkerRepository()
        request.app.state.algo_worker_repository = repository
    return repository


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Worker bearer token required")
    return token.strip()


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


def _require_action(token: WorkerToken, action: str) -> None:
    if action not in set(token.allowed_actions):
        raise HTTPException(status_code=403, detail=f"Worker token is not allowed to perform '{action}'")


def _require_v1_mode(mode: str) -> None:
    normalized = str(mode or "").strip().lower()
    if normalized not in ALLOWED_V1_MODES:
        raise HTTPException(status_code=403, detail="Algo worker API v1 allows only paper and dry_run execution modes")


def _assert_run_access(token: WorkerToken, run: Dict[str, Any]) -> None:
    if token.account_scope and run.get("account_scope") != token.account_scope:
        raise HTTPException(status_code=403, detail="Worker token cannot access this account scope")
    if token.allowed_templates and run.get("template_id") not in token.allowed_templates:
        raise HTTPException(status_code=403, detail="Worker token cannot access this strategy template")


@router.post("/tokens", response_model=WorkerTokenCreateResponse)
async def create_worker_token(request: Request, payload: WorkerTokenCreateRequest):
    require_app_user(request)
    modes = {mode.lower() for mode in payload.allowed_modes}
    if not modes or not modes.issubset(ALLOWED_V1_MODES):
        raise HTTPException(status_code=400, detail="Worker tokens may only allow paper and dry_run modes in v1")
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
    if token.account_scope and payload.account_scope != token.account_scope:
        raise HTTPException(status_code=403, detail="Worker token cannot create runs for this account scope")
    if token.allowed_templates and payload.template_id not in token.allowed_templates:
        raise HTTPException(status_code=403, detail="Worker token cannot create this strategy template")
    if payload.execution_mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot use this execution mode")

    strategy_run_id = payload.strategy_run_id or f"run_{uuid.uuid4().hex}"
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


@router.post("/worker/runs/{strategy_run_id}/intents")
async def submit_worker_intent(request: Request, strategy_run_id: str, payload: WorkerIntentRequest):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    mode = str(run.get("execution_mode") or "").lower()
    _require_v1_mode(mode)
    if mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot submit intents for this execution mode")

    existing = await _repo(request).get_intent_result(strategy_run_id, payload.idempotency_key)
    if existing is not None:
        return {"status": "deduped", "result": existing}

    result: Dict[str, Any]
    if mode == "dry_run":
        result = {
            "mode": "dry_run",
            "status": "accepted",
            "intent_type": payload.intent_type,
            "payload": payload.payload,
        }
    else:
        paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
        if paper_runtime_service is None:
            raise HTTPException(status_code=503, detail="Paper runtime is not available")
        attribution = {
            "source": "algo_worker",
            "token_id": token.token_id,
            "strategy_run_id": strategy_run_id,
            "strategy_id": strategy_run_id,
            "template_id": run.get("template_id"),
            **payload.metadata,
        }
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

    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    result = await paper_runtime_service.exit_strategy(account_scope=str(run["account_scope"]), strategy_id=strategy_run_id)
    updated = await _repo(request).update_run_status(strategy_run_id, "closed", state_patch={"exit_result": result, "exit_reason": payload.reason})
    return {"mode": "paper", "status": "closed", "result": result, "run": updated}
