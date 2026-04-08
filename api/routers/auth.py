from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from algo_runtime.admin import list_instances as list_algo_runtime_instances_impl
from algo_runtime.admin import refresh_runtime as refresh_algo_runtime_impl
from algo_runtime.admin import update_instance_status as update_algo_runtime_instance_status_impl
from algo_runtime.admin import upsert_instance as upsert_algo_runtime_instance_impl
from algo_runtime.models import AlgoInstance, AlgoLifecycleState, DependencySpec, ExecutionMode
from auth_service import (
    AppUser,
    clear_auth_cookies,
    get_configured_app_username,
    get_optional_app_user,
    get_refresh_user,
    issue_auth_cookies,
    require_app_user,
    verify_app_credentials,
)
from broker_api import broker_api
from broker_api.kite_session import KiteSession
from database import get_db
from runtime_monitor import get_components, get_logs, get_meta


router = APIRouter()


class AppLoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AppUserResponse(BaseModel):
    username: str
    role: str


class AlgoRuntimeInstanceUpsertRequest(BaseModel):
    instance_id: str = Field(min_length=1)
    algo_type: str = Field(min_length=1)
    status: AlgoLifecycleState = AlgoLifecycleState.ENABLED
    execution_mode: ExecutionMode = ExecutionMode.LIVE
    config: dict = Field(default_factory=dict)
    dependency_spec: DependencySpec = Field(default_factory=DependencySpec)
    metadata: dict = Field(default_factory=dict)


class AlgoRuntimeInstanceStatusRequest(BaseModel):
    status: AlgoLifecycleState


class PaperAccountResetRequest(BaseModel):
    starting_balance: float | None = None
    force: bool = False


class PaperAccountUpsertRequest(BaseModel):
    starting_balance: float | None = None


async def _active_paper_instance_ids_for_scope(request: Request, account_scope: str) -> list[str] | None:
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    if not algo_runtime_service:
        return None
    repository = getattr(getattr(algo_runtime_service, "kernel", None), "repository", None)
    if repository is None:
        return None

    active_instances = await repository.list_active_instances()
    normalized_scope = str(account_scope or "").strip()
    blocking_statuses = {AlgoLifecycleState.ENABLED, AlgoLifecycleState.RUNNING}
    blocked_instance_ids = [
        instance.instance_id
        for instance in active_instances
        if instance.execution_mode == ExecutionMode.PAPER
        and instance.status in blocking_statuses
        and str(getattr(instance.dependency_spec, "account_scope", "") or "").strip() == normalized_scope
    ]
    return sorted(set(blocked_instance_ids))


@router.post("/auth/login", tags=["Authentication"])
def app_login(payload: AppLoginRequest, request: Request, response: Response):
    if not verify_app_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    user = AppUser(username=payload.username, role="admin")
    issue_auth_cookies(response, request, user)
    return {"user": {"username": user.username, "role": user.role}}


@router.post("/auth/refresh", tags=["Authentication"])
def app_refresh(request: Request, response: Response):
    user = get_refresh_user(request)
    issue_auth_cookies(response, request, user)
    return {"user": {"username": user.username, "role": user.role}}


@router.post("/auth/logout", tags=["Authentication"])
def app_logout(response: Response):
    clear_auth_cookies(response)
    return {"message": "Logged out"}


@router.get("/auth/me", response_model=AppUserResponse, tags=["Authentication"])
def app_me(request: Request):
    user = require_app_user(request)
    return AppUserResponse(username=user.username, role=user.role)


@router.get("/auth/session-status", tags=["Authentication"])
async def session_status(request: Request, db: Session = Depends(get_db)):
    user = require_app_user(request)
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    broker_connected = False
    if sid:
        broker_connected = db.query(KiteSession).filter_by(session_id=sid).first() is not None
    market_data_runtime = getattr(request.app.state, "market_data_runtime", None)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    paper_market_engine = getattr(request.app.state, "paper_market_engine", None)
    daily_gate = getattr(request.app.state, "daily_token_ready", None)
    algo_runtime = await algo_runtime_service.status() if algo_runtime_service else None
    return {
        "app": {
            "authenticated": user is not None,
            "user": {"username": user.username, "role": user.role} if user else None,
            "configured_admin": get_configured_app_username(),
        },
        "broker": {
            "connected": broker_connected,
            "session_id": sid if broker_connected else None,
        },
        "runtime": {
            "components": get_components(),
            "meta": get_meta(),
            "websocket": {
                "status": market_data_runtime.get_websocket_status() if market_data_runtime else "unavailable",
                "last_order_update_at": getattr(market_data_runtime, "last_order_update_at", None),
            },
            "daily_token_gate": {
                "ready": bool(daily_gate.is_set()) if daily_gate else False,
            },
            "algo_runtime": algo_runtime,
            "paper_runtime": {
                "available": paper_runtime_service is not None,
                "market_engine": paper_market_engine.status() if paper_market_engine else None,
            },
        },
    }


@router.get("/system/runtime", tags=["System"])
async def runtime_status(request: Request):
    require_app_user(request)
    market_data_runtime = getattr(request.app.state, "market_data_runtime", None)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    paper_market_engine = getattr(request.app.state, "paper_market_engine", None)
    daily_gate = getattr(request.app.state, "daily_token_ready", None)
    return {
        "components": get_components(),
        "meta": get_meta(),
        "websocket": {
            "status": market_data_runtime.get_websocket_status() if market_data_runtime else "unavailable",
            "last_order_update_at": getattr(market_data_runtime, "last_order_update_at", None),
        },
        "daily_token_gate": {
            "ready": bool(daily_gate.is_set()) if daily_gate else False,
        },
        "algo_runtime": await algo_runtime_service.status() if algo_runtime_service else None,
        "paper_runtime": {
            "available": paper_runtime_service is not None,
            "market_engine": paper_market_engine.status() if paper_market_engine else None,
        },
    }


@router.get("/system/algo-runtime/instances", tags=["System"])
async def list_algo_runtime_instances(request: Request):
    require_app_user(request)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    if not algo_runtime_service:
        raise HTTPException(status_code=503, detail="Algo runtime is not available")
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    return await list_algo_runtime_instances_impl(algo_runtime_service, live_worker=live_worker)


@router.post("/system/algo-runtime/instances/upsert", tags=["System"])
async def upsert_algo_runtime_instance(request: Request, payload: AlgoRuntimeInstanceUpsertRequest):
    require_app_user(request)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    if not algo_runtime_service:
        raise HTTPException(status_code=503, detail="Algo runtime is not available")
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    instance = AlgoInstance(
        instance_id=payload.instance_id,
        algo_type=payload.algo_type,
        status=payload.status,
        execution_mode=payload.execution_mode,
        config=payload.config,
        dependency_spec=payload.dependency_spec,
        metadata=payload.metadata,
    )
    return await upsert_algo_runtime_instance_impl(algo_runtime_service, instance, live_worker=live_worker)


@router.post("/system/algo-runtime/instances/{instance_id}/status", tags=["System"])
async def update_algo_runtime_instance_status(request: Request, instance_id: str, payload: AlgoRuntimeInstanceStatusRequest):
    require_app_user(request)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    if not algo_runtime_service:
        raise HTTPException(status_code=503, detail="Algo runtime is not available")
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    updated = await update_algo_runtime_instance_status_impl(
        algo_runtime_service,
        instance_id=instance_id,
        status=payload.status,
        live_worker=live_worker,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Algo runtime instance not found")
    return updated


@router.post("/system/algo-runtime/refresh", tags=["System"])
async def refresh_algo_runtime(request: Request):
    require_app_user(request)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    if not algo_runtime_service:
        raise HTTPException(status_code=503, detail="Algo runtime is not available")
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    return await refresh_algo_runtime_impl(algo_runtime_service, live_worker=live_worker)


@router.get("/system/paper/accounts/{account_scope}", tags=["System"])
async def get_paper_account_summary(request: Request, account_scope: str):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    return await paper_runtime_service.get_account_summary(account_scope)


@router.post("/system/paper/accounts/{account_scope}/reset", tags=["System"])
async def reset_paper_account(request: Request, account_scope: str, payload: PaperAccountResetRequest):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    paper_market_engine = getattr(request.app.state, "paper_market_engine", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")

    if not payload.force:
        active_instance_ids = await _active_paper_instance_ids_for_scope(request, account_scope)
        if active_instance_ids is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Reset blocked: algo runtime visibility is unavailable; retry later or use force=true if you intentionally want to bypass the guard",
                    "account_scope": account_scope,
                },
            )
        if active_instance_ids:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Reset blocked: active paper algo instances are using this account_scope",
                    "account_scope": account_scope,
                    "active_instance_ids": active_instance_ids,
                },
            )

    result = await paper_runtime_service.reset_account(account_scope, starting_balance=payload.starting_balance)
    if paper_market_engine:
        await paper_market_engine.sync_subscriptions()
    return result


@router.post("/system/paper/accounts/{account_scope}/upsert", tags=["System"])
async def upsert_paper_account(request: Request, account_scope: str, payload: PaperAccountUpsertRequest):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    account = await paper_runtime_service.ensure_account(account_scope, starting_balance=payload.starting_balance)
    return {"account": account.model_dump(mode="json")}


@router.get("/system/paper/orders", tags=["System"])
async def list_paper_orders(
    request: Request,
    account_scope: str = Query(...),
    strategy_tag: Optional[str] = Query(default=None),
    algo_instance_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    items = await paper_runtime_service.list_orders(
        account_scope,
        strategy_tag=strategy_tag,
        algo_instance_id=algo_instance_id,
        limit=limit,
    )
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/system/paper/trades", tags=["System"])
async def list_paper_trades(
    request: Request,
    account_scope: str = Query(...),
    strategy_tag: Optional[str] = Query(default=None),
    algo_instance_id: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    items = await paper_runtime_service.list_trades(
        account_scope,
        strategy_tag=strategy_tag,
        algo_instance_id=algo_instance_id,
        limit=limit,
    )
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/system/paper/positions", tags=["System"])
async def list_paper_positions(
    request: Request,
    account_scope: str = Query(...),
    only_open: bool = Query(default=False),
):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    items = await paper_runtime_service.list_positions(account_scope, only_open=only_open)
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/system/logs", tags=["System"])
def runtime_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    level: Optional[str] = Query(default=None),
):
    require_app_user(request)
    return {"items": get_logs(limit=limit, level=level)}


@router.get("/system/broker-login-health", tags=["System"])
def broker_login_health(request: Request, db: Session = Depends(get_db)):
    require_app_user(request)

    meta = get_meta()
    broker_meta = meta.get("daily_broker_login", {}) or {}
    scheduler_meta = meta.get("daily_token_scheduler", {}) or {}
    gate_meta = meta.get("daily_token_gate", {}) or {}
    scheduler_component = get_components().get("daily_token_scheduler", {}) or {}

    system_session = db.query(KiteSession).filter_by(session_id="system").first()
    status = broker_meta["status"] if "status" in broker_meta else scheduler_component.get("status", "unknown")
    token_suffix = None
    if system_session and getattr(system_session, "access_token", None):
        token_suffix = system_session.access_token[-6:]
    elif broker_meta.get("token_suffix"):
        token_suffix = broker_meta.get("token_suffix")

    return {
        "status": status,
        "automation": {
            "enabled": True,
            "mode": broker_meta.get("mode", "headless_login_with_stored_totp"),
            "requires_manual_totp_entry": False,
        },
        "system_session": {
            "present": system_session is not None,
            "updated_at": system_session.created_at.isoformat() if system_session and system_session.created_at else None,
            "token_suffix": token_suffix,
        },
        "last_login": {
            "last_success_at": broker_meta.get("last_success_at"),
            "last_failure_at": broker_meta.get("last_failure_at"),
            "last_error": broker_meta.get("last_error"),
            "attempts": broker_meta.get("attempts"),
        },
        "scheduler": {
            "status": scheduler_component.get("status", "unknown"),
            "detail": scheduler_component.get("detail"),
            "next_run": scheduler_meta.get("next_run"),
            "sleep_seconds": scheduler_meta.get("sleep_seconds"),
            "last_heartbeat": scheduler_component.get("last_heartbeat") or scheduler_meta.get("last_heartbeat"),
        },
        "gate": {
            "ready": bool(getattr(getattr(request.app.state, "daily_token_ready", None), "is_set", lambda: False)()),
            "last_changed_at": gate_meta.get("last_changed_at"),
        },
        "notes": [
            "Automatic login uses stored Kite credentials and TOTP secret on the backend.",
            "This avoids daily manual TOTP entry, but still depends on Zerodha's web login flow remaining compatible.",
        ],
    }


router.add_api_route("/login_kite", broker_api.headless_login, methods=["POST"], tags=["Authentication"])
router.add_api_route("/logout_kite", broker_api.logout, methods=["POST"], tags=["Authentication"])
router.add_api_route("/profile_kite", broker_api.profile, methods=["GET"], tags=["Authentication"])
router.add_api_route("/holdings_kite", broker_api.holdings, methods=["GET"], tags=["Authentication"])
router.add_api_route("/margins", broker_api.get_margins, methods=["GET"], tags=["Authentication"])
