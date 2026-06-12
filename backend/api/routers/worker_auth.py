from __future__ import annotations

import logging
import secrets
import uuid
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from backend.algo_runtime.account_scope import parse_account_scope
from backend.app.auth import require_app_user
from backend.api.schemas.worker import WorkerTokenCreateRequest, WorkerTokenCreateResponse, WorkerTokenView, WorkerHeartbeatRequest, WorkerRunCreateRequest
from backend.api.routers.worker_shared import *

router = APIRouter(prefix='/algo-workers', tags=['Algo Workers'])
logger = logging.getLogger(__name__)

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

async def list_worker_tokens(request: Request):
    require_app_user(request)
    return await _repo(request).list_tokens()

async def revoke_worker_token(request: Request, token_id: str):
    require_app_user(request)
    record = await _repo(request).revoke_token(token_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Worker token not found")
    return record

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

async def worker_heartbeat(request: Request, payload: WorkerHeartbeatRequest):
    token = await require_worker_token(request)
    _require_action(token, "heartbeat")
    return await _repo(request).record_heartbeat(token.token_id, payload)

async def claim_worker_run_session(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    try:
        claimed = await _repo(request).claim_run_session(
            strategy_run_id,
            freshness_seconds=WORKER_SESSION_FRESHNESS_SECONDS,
            claimed_without_heartbeat_seconds=WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
        )
    except SQLAlchemyError as exc:
        logger.exception("algo_worker_claim_session_database_failed", extra={"strategy_run_id": strategy_run_id})
        raise HTTPException(status_code=503, detail="Worker session persistence unavailable") from exc
    if claimed is None:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_CONFLICT",
                "strategy_run_id": strategy_run_id,
            },
        )
    return {
        "strategy_run_id": strategy_run_id,
        "worker_session_nonce": claimed.get("worker_session_nonce"),
        "worker_session_claimed_at": claimed.get("worker_session_claimed_at"),
    }

async def release_worker_run_session(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    nonce = _worker_session_nonce_from_request(request)
    if not nonce:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_REQUIRED",
                "strategy_run_id": strategy_run_id,
            },
        )
    released = await _repo(request).release_run_session(strategy_run_id, expected_nonce=nonce)
    if released is None:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_CONFLICT",
                "strategy_run_id": strategy_run_id,
            },
        )
    return {"status": "released", "strategy_run_id": strategy_run_id}

async def heartbeat_worker_run_session(request: Request, strategy_run_id: str, payload: WorkerHeartbeatRequest):
    _ = payload
    token = await require_worker_token(request)
    _require_action(token, "heartbeat")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    nonce = _worker_session_nonce_from_request(request)
    if not nonce:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_REQUIRED",
                "strategy_run_id": strategy_run_id,
            },
        )
    updated = await _repo(request).record_run_heartbeat(strategy_run_id, expected_nonce=nonce)
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_CONFLICT",
                "strategy_run_id": strategy_run_id,
            },
        )
    return {
        "status": "ok",
        "strategy_run_id": strategy_run_id,
        "last_heartbeat_at": updated.get("last_heartbeat_at"),
    }

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
        from backend.api.routers.worker_protection import _initial_backend_protection_state, _normalized_backend_protection_runtime_state

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

    try:
        return await _repo(request).create_run(token, payload, strategy_run_id=strategy_run_id)
    except IntegrityError as exc:
        logger.warning(
            "algo_worker_run_create_conflict",
            extra={
                "strategy_run_id": strategy_run_id,
                "account_scope": payload.account_scope,
                "execution_mode": payload.execution_mode,
                "template_id": payload.template_id,
            },
        )
        raise HTTPException(status_code=409, detail="Strategy run already exists") from exc
    except SQLAlchemyError as exc:
        logger.exception(
            "algo_worker_run_create_database_failed",
            extra={
                "strategy_run_id": strategy_run_id,
                "account_scope": payload.account_scope,
                "execution_mode": payload.execution_mode,
                "template_id": payload.template_id,
            },
        )
        raise HTTPException(status_code=503, detail="Worker run persistence unavailable") from exc


async def _attach_worker_run_positions(request: Request, run: dict) -> dict:
    enriched = dict(run)
    strategy_run_id = str(enriched.get("strategy_run_id") or "")
    execution_mode = str(enriched.get("execution_mode") or "").strip().lower()
    account_scope = str(enriched.get("account_scope") or "")
    positions = []
    source = "none"
    status = "available"

    try:
        if execution_mode == "paper":
            paper_runtime = getattr(request.app.state, "paper_runtime_service", None)
            if paper_runtime is not None and hasattr(paper_runtime, "get_strategy_run_pnl"):
                pnl = await paper_runtime.get_strategy_run_pnl(account_scope, strategy_run_id)
                if isinstance(pnl, dict):
                    positions = list(pnl.get("positions") or pnl.get("legs") or [])
                    source = "paper_runtime"
            else:
                source = "paper_runtime_unavailable"
        elif execution_mode == "live":
            positions = await _repo(request).list_live_strategy_broker_positions(
                strategy_run_id=strategy_run_id,
                account_id=account_scope,
            )
            source = "live_order_attribution"
        else:
            source = "dry_run"
    except Exception as exc:
        logger.warning(
            "algo_worker_run_positions_unavailable",
            extra={"strategy_run_id": strategy_run_id, "account_scope": account_scope, "execution_mode": execution_mode, "error": str(exc)},
        )
        positions = []
        status = "unavailable"

    serialized_positions = [_serialize_model(position) for position in positions]
    enriched["positions"] = serialized_positions
    enriched["backend_positions"] = serialized_positions
    enriched["backend_positions_status"] = status
    enriched["backend_positions_source"] = source
    return enriched


async def get_worker_run(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    return await _attach_worker_run_positions(request, _enrich_run_health_fields(run))


router.add_api_route("/tokens", create_worker_token, methods=["POST"], response_model=WorkerTokenCreateResponse)
router.add_api_route("/tokens", list_worker_tokens, methods=["GET"], response_model=list[WorkerTokenView])
router.add_api_route("/tokens/{token_id}/revoke", revoke_worker_token, methods=["POST"], response_model=WorkerTokenView)
router.add_api_route("/worker/health", worker_health, methods=["GET"])
router.add_api_route("/worker/heartbeat", worker_heartbeat, methods=["POST"])
router.add_api_route("/worker/runs/{strategy_run_id}/claim-session", claim_worker_run_session, methods=["POST"])
router.add_api_route("/worker/runs/{strategy_run_id}/claim-session", release_worker_run_session, methods=["DELETE"])
router.add_api_route("/worker/runs/{strategy_run_id}/heartbeat", heartbeat_worker_run_session, methods=["POST"])
router.add_api_route("/worker/runs", create_worker_run, methods=["POST"])
router.add_api_route("/worker/runs/{strategy_run_id}", get_worker_run, methods=["GET"])
