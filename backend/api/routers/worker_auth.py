from __future__ import annotations

import logging
import secrets
import uuid
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from backend.algo_runtime.account_scope import parse_account_scope
from backend.app.auth import require_app_user
from backend.api.schemas.worker import (
    WorkerHeartbeatRequest,
    WorkerRunCreateRequest,
    WorkerRunListResponse,
    WorkerTokenCreateRequest,
    WorkerTokenCreateResponse,
    WorkerTokenView,
)
from backend.api.routers.worker_shared import *
from backend.api.services.hosted_attempt import assert_child_lifecycle_forbidden

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


async def list_worker_runs(request: Request, limit: int = 25, cursor: str | None = None):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    try:
        return await _repo(request).list_runs(token, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("algo_worker_run_list_database_failed")
        raise HTTPException(status_code=503, detail="Worker run listing unavailable") from exc

async def claim_worker_run_session(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    assert_child_lifecycle_forbidden(run, "claim_session")
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
    assert_child_lifecycle_forbidden(run, "release_session")
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
    assert_child_lifecycle_forbidden(run, "heartbeat_session")
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
    return await create_worker_run_for_token(request, token, payload)


async def _attach_worker_run_positions(request: Request, run: dict) -> dict:
    enriched = dict(run)
    strategy_run_id = str(enriched.get("strategy_run_id") or "")
    execution_mode = str(enriched.get("execution_mode") or "").strip().lower()
    account_scope = str(enriched.get("account_scope") or "")

    # The run's canonical strategy identity comes from the PERSISTED binding, so
    # a strategy never has to guess its id or be handed one as a parameter. An
    # unbound (legacy/external) run reports ``unattributed`` rather than a guess,
    # and a binding that disagrees with this run's own account/environment is a
    # mismatch rather than an attribution.
    enriched["strategy_attribution"] = _run_strategy_attribution(
        request,
        strategy_run_id,
        account_scope=account_scope,
        execution_environment=execution_mode,
    )

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


def _strategies_session_factory(request: Request):
    """The hosted-strategy session factory, resolved as the other worker routes do.

    Production never sets ``app.state.strategies_session_factory``; only tests
    and the isolated acceptance apps do. Falling back to the ordinary
    ``SessionLocal`` (exactly like ``worker_proposals``/``worker_executions``)
    keeps the normal production path working instead of reporting every run as
    unattributed.
    """
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _run_strategy_attribution(
    request: Request,
    strategy_run_id: str,
    *,
    account_scope: str = "",
    execution_environment: str = "",
) -> Any:
    """``{strategy_id, owner_id, account_id, execution_environment}`` or a reason.

    The persisted binding is authoritative only where it AGREES with the run the
    caller already authenticated against: a binding whose account or environment
    belongs to something else is a data mismatch, not a licence to attribute this
    run to another strategy.
    """
    if not strategy_run_id:
        return "unattributed"
    session_factory = _strategies_session_factory(request)
    from sqlalchemy import select

    from backend.strategies.attribution_models import StrategyRunBinding

    try:
        with session_factory() as session:
            row = session.execute(
                select(
                    StrategyRunBinding.strategy_id,
                    StrategyRunBinding.owner_id,
                    StrategyRunBinding.account_id,
                    StrategyRunBinding.execution_environment,
                    StrategyRunBinding.binding_source,
                ).where(StrategyRunBinding.strategy_run_id == str(strategy_run_id))
            ).first()
    except Exception:  # noqa: BLE001 - an unreadable binding is "unknown", not a guess
        logger.warning(
            "algo_worker_run_attribution_unavailable",
            extra={"strategy_run_id": strategy_run_id},
        )
        return "unattributed"
    if row is None:
        return "unattributed"
    if account_scope and str(row[2]) != str(account_scope):
        logger.warning(
            "algo_worker_run_attribution_account_mismatch",
            extra={
                "strategy_run_id": strategy_run_id,
                "run_account_scope": str(account_scope),
                "binding_account_id": str(row[2]),
            },
        )
        return "unattributed"
    if execution_environment and str(row[3]) != str(execution_environment):
        logger.warning(
            "algo_worker_run_attribution_environment_mismatch",
            extra={
                "strategy_run_id": strategy_run_id,
                "run_execution_mode": str(execution_environment),
                "binding_execution_environment": str(row[3]),
            },
        )
        return "unattributed"
    return {
        "strategy_id": str(row[0]),
        "owner_id": str(row[1]),
        "account_id": str(row[2]),
        "execution_environment": str(row[3]),
        "binding_source": str(row[4]),
    }


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
router.add_api_route("/worker/runs", list_worker_runs, methods=["GET"], response_model=WorkerRunListResponse)
router.add_api_route("/worker/runs/{strategy_run_id}", get_worker_run, methods=["GET"])
