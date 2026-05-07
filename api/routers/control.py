from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from kiteconnect import KiteConnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.services.control_plane import (
    build_strategy_positions_snapshot,
    cancel_control_strategy_orders,
    exit_control_strategy,
)
from app.auth import require_app_user
from broker_api.orders import get_correlation_id
from broker_api.session.kite_session import get_kite, get_kite_session_id, get_session_account_id
from app.database import get_db


router = APIRouter(prefix="/control", tags=["Control Plane"])


class ControlActionRequest(BaseModel):
    reason: str | None = None
    dry_run: bool = False
    account_scope: str = "default"


@router.get("/strategy-positions")
async def get_strategy_positions(
    request: Request,
    account_scope: str = Query("default"),
    broker_account_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    require_app_user(request)
    resolved_broker_account_id = broker_account_id
    if not resolved_broker_account_id:
        session_id = get_kite_session_id(request)
        if session_id:
            resolved_broker_account_id = get_session_account_id(db, session_id)
    return await build_strategy_positions_snapshot(
        request,
        account_scope=account_scope,
        broker_account_id=resolved_broker_account_id,
    )


@router.post("/strategies/{strategy_run_id}/exit")
async def exit_strategy(request: Request, strategy_run_id: str, payload: ControlActionRequest):
    require_app_user(request)
    return await exit_control_strategy(
        request,
        strategy_run_id,
        account_scope=payload.account_scope,
        reason=payload.reason,
        dry_run=payload.dry_run,
    )


@router.post("/strategies/{strategy_run_id}/cancel-orders")
async def cancel_strategy_orders(request: Request, strategy_run_id: str, payload: ControlActionRequest):
    require_app_user(request)
    return await cancel_control_strategy_orders(request, strategy_run_id, reason=payload.reason)


@router.post("/reconcile")
async def reconcile_control_plane(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id),
):
    require_app_user(request)
    from broker_api.orders import reconcile_realtime_positions

    return await reconcile_realtime_positions(request, kite=kite, db=db, corr_id=corr_id)
