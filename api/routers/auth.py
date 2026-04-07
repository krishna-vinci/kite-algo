from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

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
        },
    }


@router.get("/system/runtime", tags=["System"])
async def runtime_status(request: Request):
    require_app_user(request)
    market_data_runtime = getattr(request.app.state, "market_data_runtime", None)
    algo_runtime_service = getattr(request.app.state, "algo_runtime_service", None)
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
    }


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
