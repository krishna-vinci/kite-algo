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
from strategies.option_strategy.store import OptionStrategyStore
from runtime_monitor import get_components, get_logs, get_meta
from strategies.option_strategy.runtime_updates import apply_protection_patch, build_option_run_capabilities, build_option_run_summary_fields


router = APIRouter()


def _build_system_broker_status(request: Request, db: Session) -> dict:
    meta = get_meta()
    broker_meta = meta.get("daily_broker_login", {}) or {}
    scheduler_meta = meta.get("daily_token_scheduler", {}) or {}
    scheduler_component = get_components().get("daily_token_scheduler", {}) or {}
    gate_ready = bool(getattr(getattr(request.app.state, "daily_token_ready", None), "is_set", lambda: False)())

    system_session = db.query(KiteSession).filter_by(session_id="system").first()
    raw_status = str(broker_meta.get("status") or scheduler_component.get("status") or "unknown").lower()
    scheduler_status = str(scheduler_component.get("status") or "unknown").lower()

    if scheduler_status == "running":
        status = "reconnecting"
    elif raw_status == "degraded":
        status = "degraded"
    elif system_session and gate_ready:
        status = "connected"
    elif system_session:
        status = "reconnecting"
    else:
        status = "disconnected"

    token_suffix = None
    if system_session and getattr(system_session, "access_token", None):
        token_suffix = system_session.access_token[-6:]
    elif broker_meta.get("token_suffix"):
        token_suffix = broker_meta.get("token_suffix")

    return {
        "connected": status == "connected",
        "status": status,
        "mode": "system",
        "automation": {
            "enabled": True,
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
            "status": scheduler_status,
            "detail": scheduler_component.get("detail"),
            "next_run": scheduler_meta.get("next_run"),
            "sleep_seconds": scheduler_meta.get("sleep_seconds"),
            "last_heartbeat": scheduler_component.get("last_heartbeat") or scheduler_meta.get("last_heartbeat"),
        },
        "gate": {
            "ready": gate_ready,
        },
    }


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


class PaperOrderPayload(BaseModel):
    exchange: str = Field(min_length=1)
    tradingsymbol: str = Field(min_length=1)
    product: str = Field(min_length=1)
    transaction_type: str = Field(min_length=1)
    order_type: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    price: float | None = None
    trigger_price: float | None = None


class PaperBasketRequest(BaseModel):
    orders: list[PaperOrderPayload] = Field(min_length=1)
    all_or_none: bool = True
    strategy_tag: str | None = None
    notes: str | None = None


class PaperStrategyExitRequest(BaseModel):
    strategy_id: str = Field(min_length=1)


class PaperStrategyRiskUpdateRequest(BaseModel):
    combined_premium_target: float | None = None
    combined_premium_stoploss: float | None = None
    basket_mtm_target: float | None = None
    basket_mtm_stoploss: float | None = None
    index_lower_boundary: float | None = None
    index_upper_boundary: float | None = None


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
    user = get_optional_app_user(request)
    broker_status = _build_system_broker_status(request, db)
    broker = {
        "connected": broker_status["connected"],
        "status": broker_status["status"],
        "mode": broker_status["mode"],
        "last_login": {
            "last_success_at": broker_status["last_login"]["last_success_at"],
            "last_failure_at": broker_status["last_login"]["last_failure_at"] if user else None,
            "last_error": broker_status["last_login"]["last_error"] if user else None,
        },
        "scheduler": {
            "next_run": broker_status["scheduler"]["next_run"],
        },
    }
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
            **broker,
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


@router.post("/system/paper/accounts/{account_scope}/basket", tags=["System"])
async def place_paper_basket(request: Request, account_scope: str, payload: PaperBasketRequest):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")

    result = await paper_runtime_service.place_basket(
        account_scope=account_scope,
        basket_payload={
            "orders": [order.model_dump(mode="json") for order in payload.orders],
            "all_or_none": payload.all_or_none,
        },
        attribution={
            "source": "frontend-next-options",
            "strategy_tag": payload.strategy_tag,
            "notes": payload.notes,
        },
    )
    return result


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


@router.get("/system/paper/strategies", tags=["System"])
async def list_paper_strategies(request: Request, account_scope: str = Query(...)):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")

    summary = await paper_runtime_service.get_strategy_summary(account_scope)
    option_strategy_store = getattr(request.app.state, "option_strategy_store", None)
    if option_strategy_store is None:
        option_strategy_store = OptionStrategyStore(journal_service=getattr(request.app.state, "journal_service", None))
        request.app.state.option_strategy_store = option_strategy_store

    for strategy in summary.get("strategies", []):
        run_id = strategy.get("strategy_id")
        if not run_id:
            continue
        if str(run_id).startswith("manual:"):
            strategy["capabilities"] = {
                **dict(strategy.get("capabilities") or {}),
                "can_edit_risk": False,
                "edit_risk_reason": "Manual paper activity does not support runtime risk edits",
                "can_exit_strategy": False,
                "exit_reason": "Strategy-level exit is unavailable for manual paper activity",
                "allowed_actions": [],
                "risk_schema": [],
            }
            strategy["summary_fields"] = []
            continue
        if str(run_id).startswith("unsupported:"):
            strategy["capabilities"] = {
                **dict(strategy.get("capabilities") or {}),
                "can_edit_risk": False,
                "edit_risk_reason": "Unsupported shared positions do not support runtime risk edits",
                "can_exit_strategy": False,
                "exit_reason": strategy.get("capabilities", {}).get("exit_reason")
                or "Strategy-level exit is disabled because the open position attribution is ambiguous",
                "allowed_actions": [],
                "risk_schema": [],
            }
            strategy["summary_fields"] = []
            continue
        run = option_strategy_store.get_strategy_run(str(run_id)) if hasattr(option_strategy_store, "get_strategy_run") else option_strategy_store.get_run(str(run_id))
        if run is None:
            strategy["capabilities"] = {
                **dict(strategy.get("capabilities") or {}),
                "can_edit_risk": False,
                "edit_risk_reason": "Runtime monitoring is unavailable for this paper strategy",
                "can_exit_strategy": False,
                "exit_reason": "Strategy-level exit requires a monitored paper strategy",
                "allowed_actions": [],
                "risk_schema": [],
            }
            strategy["summary_fields"] = []
            continue
        canonical = dict(run.get("canonical_strategy") or {})
        strategy["mode"] = run.get("execution_mode") or strategy.get("mode") or "paper"
        strategy["summary_fields"] = build_option_run_summary_fields(canonical)
        strategy["capabilities"] = build_option_run_capabilities(
            run,
            canonical_strategy=canonical,
            is_open=bool(strategy.get("is_open")),
            mode=str(strategy.get("mode") or "paper"),
        )
    return summary


@router.patch("/system/paper/strategies/{strategy_id}/risk", tags=["System"])
async def update_paper_strategy_risk(
    request: Request,
    strategy_id: str,
    payload: PaperStrategyRiskUpdateRequest,
    account_scope: str = Query(default="default"),
):
    require_app_user(request)
    option_strategy_store = getattr(request.app.state, "option_strategy_store", None)
    if option_strategy_store is None:
        option_strategy_store = OptionStrategyStore(journal_service=getattr(request.app.state, "journal_service", None))
        request.app.state.option_strategy_store = option_strategy_store

    run = option_strategy_store.get_strategy_run(strategy_id) if hasattr(option_strategy_store, "get_strategy_run") else option_strategy_store.get_run(strategy_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Option strategy not found")
    if str(run.get("execution_mode") or "") != "paper":
        raise HTTPException(status_code=409, detail="Only paper strategies can be edited from this route")

    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    summary = await paper_runtime_service.get_strategy_summary(account_scope)
    strategy = next((item for item in summary.get("strategies", []) if str(item.get("strategy_id")) == strategy_id), None)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Paper strategy summary not found")
    if not bool(strategy.get("is_open")):
        raise HTTPException(status_code=409, detail="Risk can only be edited while the strategy is open")

    patch = {field: getattr(payload, field) for field in payload.model_fields_set}
    updated = apply_protection_patch(run, patch)
    option_strategy_store.update_canonical_strategy(strategy_id, canonical_strategy=updated["canonical_strategy"])

    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    algo_instance_id = run.get("algo_instance_id")
    if algo_runtime_service and algo_instance_id:
        existing_instance = await algo_runtime_service.kernel.repository.get_instance(str(algo_instance_id))
        if existing_instance is not None:
            next_config = dict(existing_instance.config or {})
            next_config.update(updated["runtime_config"])
            await upsert_algo_runtime_instance_impl(
                algo_runtime_service,
                existing_instance.model_copy(update={"config": next_config}),
                live_worker=live_worker,
            )

    return {
        "strategy_id": strategy_id,
        **updated,
    }


@router.post("/system/paper/accounts/{account_scope}/exit-strategy", tags=["System"])
async def exit_paper_strategy(request: Request, account_scope: str, payload: PaperStrategyExitRequest):
    require_app_user(request)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if not paper_runtime_service:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")

    normalized_strategy_id = str(payload.strategy_id or "").strip()
    if normalized_strategy_id.startswith("manual:"):
        raise HTTPException(status_code=409, detail="Strategy-level exit is unavailable for manual paper activity")
    if normalized_strategy_id.startswith("unsupported:"):
        raise HTTPException(status_code=409, detail="Strategy-level exit is disabled because the open position attribution is ambiguous")

    option_strategy_store = getattr(request.app.state, "option_strategy_store", None)
    if option_strategy_store is None:
        option_strategy_store = OptionStrategyStore(journal_service=getattr(request.app.state, "journal_service", None))
        request.app.state.option_strategy_store = option_strategy_store

    strategy_run = option_strategy_store.get_strategy_run(payload.strategy_id) if hasattr(option_strategy_store, "get_strategy_run") else option_strategy_store.get_run(payload.strategy_id)
    if strategy_run is None:
        raise HTTPException(status_code=409, detail="Strategy-level exit requires a monitored paper strategy")

    result = await paper_runtime_service.exit_strategy(account_scope=account_scope, strategy_id=payload.strategy_id)

    algo_instance_id = strategy_run.get("algo_instance_id")
    if result.get("status") != "noop":
        option_strategy_store.mark_exited(payload.strategy_id, execution_result=result, algo_instance_id=algo_instance_id)
        service = getattr(request.app.state, "algo_runtime_service", None)
        if service and algo_instance_id:
            live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
            await update_algo_runtime_instance_status_impl(
                service,
                instance_id=algo_instance_id,
                status=AlgoLifecycleState.STOPPED,
                live_worker=live_worker,
            )

    return result


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
    broker_status = _build_system_broker_status(request, db)
    return {
        **broker_status,
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
