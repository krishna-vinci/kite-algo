"""Platform control plane: live settings, platform status, approvals inbox.

Three owner surfaces that span every hosted strategy rather than one:

``GET/PUT /api/platform/live-settings``
    Which live lanes may take NEW exposure. ``HOSTED_LIVE_ENABLED`` stays a
    read-only master switch (this router can never arm live trading), the lane
    map is persisted with an append-only audit row, and a lane closed here never
    blocks a reduction, an exit, the MIS square-off, repair or flatten.

``GET /api/platform/status``
    One honest state per component (broker, market data, strategy runner) plus
    the derived ``live``/``paper`` mode. A component with no evidence reports
    ``unknown``/``down`` rather than being guessed from a neighbour.

``GET /api/strategies/approvals/pending``
    Every execution request awaiting THIS owner's decision, across strategies.
    Approve/reject keep using the existing
    ``POST /api/strategies/{id}/execution-requests/{rid}/approve|reject`` routes;
    this route only reads.

Auth is cookie-only (``require_app_user``, the same server-derived ``app:<user>``
owner the hosted-strategy router uses); the PUT additionally enforces the
same-origin assertion every other cookie-authenticated mutation uses, since
``SameSite=None`` on HTTPS removes the cookie defense. No worker token opens any
route here.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.routers.strategies import require_strategy_owner
from backend.api.schemas.platform import (
    KillSwitchJobOutcome,
    KillSwitchRequest,
    KillSwitchResponse,
    KillSwitchStrategyProgress,
    PendingApprovalRow,
    PendingApprovalsResponse,
    PlatformAccountView,
    PlatformBrokerStatus,
    PlatformLanes,
    PlatformLiveSettingsResponse,
    PlatformLiveSettingsUpdateRequest,
    PlatformLiveStatus,
    PlatformMarketDataStatus,
    PlatformRiskStatus,
    PlatformStatusResponse,
    PlatformStrategyRunnerStatus,
)
from backend.api.services.csrf import enforce_same_origin
from backend.api.services.kill_switch import (
    KillSwitchRefusal,
    KillSwitchService,
)
from backend.platform.settings import (
    LIVE_LANE_KEYS,
    UNSET,
    read_live_settings,
    update_live_settings,
)
from backend.platform.status import broker_account_view, platform_status_view
from backend.strategies.execution_requests import ExecutionRequestService
from backend.strategies.live_settings import hosted_live_enabled

router = APIRouter(tags=["Platform control plane (operator)"])

__all__ = ["router"]

#: The cross-strategy approvals inbox. Deliberately a LITERAL path under
#: ``/strategies``: it is registered before the hosted-strategy router (see
#: ``backend.api.routers.ALL_ROUTERS``) so no ``/{strategy_id}`` pattern can ever
#: be considered first for it.
PENDING_APPROVALS_PATH = "/strategies/approvals/pending"


def _platform_db(request: Request):
    """Sessionmaker for the platform tables (injectable for tests)."""
    factory = getattr(request.app.state, "platform_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _settings_response(*, session_factory: Any) -> PlatformLiveSettingsResponse:
    source = os.environ
    persisted = read_live_settings(session_factory)
    if persisted is not None:
        lanes = PlatformLanes(**persisted.lanes)
        lanes_source = "db"
        updated_at = persisted.updated_at
        updated_by = persisted.updated_by
    else:
        # No stored row: the deployment's own env allowlist is what the live
        # path is honouring, so that (and not "nothing") is what is reported.
        from backend.strategies.live_service import enabled_live_lanes

        open_lanes = set(enabled_live_lanes(source))
        lanes = PlatformLanes(**{lane: (lane in open_lanes) for lane in LIVE_LANE_KEYS})
        lanes_source = "env"
        updated_at = None
        updated_by = None
    return PlatformLiveSettingsResponse(
        live_enabled=hosted_live_enabled(source),
        lanes=lanes,
        lanes_source=lanes_source,
        account_daily_loss_cap_inr=(
            persisted.account_daily_loss_cap_inr if persisted is not None else None
        ),
        account=PlatformAccountView(**broker_account_view(session_factory)),
        updated_at=updated_at,
        updated_by=updated_by,
    )


@router.get("/platform/live-settings", response_model=PlatformLiveSettingsResponse)
def get_live_settings(
    owner: str = Depends(require_strategy_owner),
    session_factory: Any = Depends(_platform_db),
):
    """The lane policy in force right now, and where it came from."""
    return _settings_response(session_factory=session_factory)


@router.put("/platform/live-settings", response_model=PlatformLiveSettingsResponse)
def update_live_settings_route(
    request: Request,
    payload: PlatformLiveSettingsUpdateRequest,
    owner: str = Depends(require_strategy_owner),
    session_factory: Any = Depends(_platform_db),
):
    """Persist the lane policy and audit the change.

    The master switch is NOT editable here, and the written row can only ever
    narrow what the deployment already allows: ``HOSTED_LIVE_ENABLED`` still has
    to be on for any live execution to happen.
    """
    enforce_same_origin(request)
    # A lanes-only PUT must not silently clear a configured cap: the cap moves
    # only when the caller names it (an explicit ``null`` clears it).
    cap_update: Any = (
        payload.account_daily_loss_cap_inr
        if "account_daily_loss_cap_inr" in payload.model_fields_set
        else UNSET
    )
    update_live_settings(
        payload.lanes.model_dump(),
        actor_id=owner,
        reason=payload.reason,
        account_daily_loss_cap_inr=cap_update,
        session_factory=session_factory,
    )
    return _settings_response(session_factory=session_factory)


@router.get("/platform/status", response_model=PlatformStatusResponse)
async def get_platform_status(
    owner: str = Depends(require_strategy_owner),
    session_factory: Any = Depends(_platform_db),
):
    """Broker, market-data and runner state, plus the derived live/paper mode."""
    body = await platform_status_view(session_factory=session_factory)
    return PlatformStatusResponse(
        mode=body["mode"],
        broker=PlatformBrokerStatus(**body["broker"]),
        market_data=PlatformMarketDataStatus(**body["market_data"]),
        strategy_runner=PlatformStrategyRunnerStatus(**body["strategy_runner"]),
        live=PlatformLiveStatus(**body["live"]),
        risk=PlatformRiskStatus(**body["risk"]),
    )


@router.get(PENDING_APPROVALS_PATH, response_model=PendingApprovalsResponse)
def list_pending_approvals(
    limit: int = 100,
    owner: str = Depends(require_strategy_owner),
    session_factory: Any = Depends(_platform_db),
):
    """Every execution request awaiting this owner's decision, newest first."""
    service = ExecutionRequestService(session_factory=session_factory)
    rows = service.list_pending_for_owner(owner, limit=max(1, min(int(limit), 500)))
    return PendingApprovalsResponse(
        items=[PendingApprovalRow(**row) for row in rows],
        count=len(rows),
    )


# ---------------------------------------------------------------------------
# Kill switch (stop everything, flatten everything, close every lane)
# ---------------------------------------------------------------------------


def _kill_switch_service(
    request: Any, session_factory: Any
) -> KillSwitchService:
    """The kill switch over the SAME governed owner actions the routes use.

    Flatten and its status read are wired through
    ``build_owner_actions_service`` so the kill switch, the per-strategy flatten
    route and stop-and-flatten share ONE orchestration, one option-exit runner
    and one reduction pipeline.
    """
    from backend.api.routers.strategy_owner_actions import build_owner_actions_service
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(session_factory)

    async def flatten(scope: Any, *, reason: str, actor: str) -> dict:
        service = build_owner_actions_service(
            request,
            session_factory,
            repo,
            strategy_id=str(scope["strategy_id"]),
            owner=str(actor),
        )
        return await service.flatten(
            scope,
            reason=str(reason or ""),
            stop_evaluator=True,
            actor=str(actor),
        )

    def flatten_status(scope: Any) -> Any:
        service = build_owner_actions_service(
            request,
            session_factory,
            repo,
            strategy_id=str(scope["strategy_id"]),
            owner=str(scope.get("owner_id") or ""),
        )
        try:
            return service.flatten_status(scope)
        except HTTPException as exc:
            if int(exc.status_code) == 404:
                return None
            raise

    return KillSwitchService(
        session_factory=session_factory,
        repository=repo,
        flatten=flatten,
        flatten_status=flatten_status,
    )


def _kill_switch_body(result: Any) -> KillSwitchResponse:
    return KillSwitchResponse(
        operation_id=str(result.get("operation_id") or ""),
        status=str(result.get("status") or ""),
        idempotent=bool(result.get("idempotent")),
        actor_id=result.get("actor_id"),
        reason=str(result.get("reason") or ""),
        created_at=result.get("created_at"),
        strategies=[
            KillSwitchStrategyProgress(**row)
            for row in (result.get("strategies") or [])
        ],
        jobs=[KillSwitchJobOutcome(**row) for row in (result.get("jobs") or [])],
        lanes_closed=result.get("lanes_closed"),
    )


@router.post("/platform/kill-switch", response_model=KillSwitchResponse)
async def start_kill_switch(
    request: Request,
    payload: KillSwitchRequest,
    owner: str = Depends(require_strategy_owner),
    session_factory: Any = Depends(_platform_db),
):
    """Stop every running job, flatten every exposed book, close every live lane.

    The bodied confirmation (``"FLATTEN ALL"``) is the operator's explicit
    intent; without it nothing moves. The operation is durable before any work
    runs, so a second POST while it is still open returns the SAME operation
    rather than starting a parallel one.
    """
    enforce_same_origin(request)
    service = _kill_switch_service(request, session_factory)
    try:
        result = await service.start(
            owner=str(owner),
            reason=str(payload.reason or ""),
            confirm=str(payload.confirm or ""),
        )
    except KillSwitchRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return _kill_switch_body(result)


@router.get("/platform/kill-switch", response_model=KillSwitchResponse)
def inspect_kill_switch(
    request: Request,
    owner: str = Depends(require_strategy_owner),
    session_factory: Any = Depends(_platform_db),
):
    """The latest kill-switch operation and its per-strategy progress, or 404."""
    service = _kill_switch_service(request, session_factory)
    result = service.latest()
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "rejection_reason": "KILL_SWITCH_OPERATION_NONE",
                "message": "no kill-switch operation has been run",
            },
        )
    return _kill_switch_body(result)
