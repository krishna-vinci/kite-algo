"""Run-bound governed execution requests and the owned-work snapshot (Phase 2).

Authorization ordering is identical to every other worker mutation route:

    token -> action scope -> run -> _assert_run_access -> hosted attempt ->
    session freshness -> authority checks -> work

and the binding is the authority: the strategy, owner, account and environment
come from the run's persisted binding and its ``strategy_jobs`` row. The payload's
``plan_id`` is confirmed against them, never used to select them.

A request is NOT a trade authorisation. It records that the strategy wants this
frozen plan executed; the owner's decision (or a matching standing grant) is what
makes it dispatchable, and the shared pipeline still admits, reserves and
approves at dispatch time.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from backend.api.routers.worker_shared import (
    _assert_run_access,
    _require_action,
    _repo,
    require_active_worker_run_session,
    require_worker_token,
)
from backend.api.schemas.execution_requests import (
    OwnedPositionsResponse,
    OwnedOptionRunRow,
    OwnedOptionRunsCoverage,
    PendingWorkRow,
    OwnedPositionRow,
    ProjectionPublication,
    RunExecutionRequestCreate,
    RunExecutionRequestListResponse,
    RunExecutionRequestResponse,
)
from backend.api.services.hosted_attempt import (
    enforce_hosted_attempt_authority,
    hosted_job_for_run,
)
from backend.strategies.execution_requests import (
    ExecutionRequestError,
    ExecutionRequestService,
)
from backend.strategies.execution_snapshot import OwnedWorkSnapshotService

router = APIRouter(prefix="/algo-workers", tags=["Algo Workers"])

#: A request needs the same capability that lets a child author a trade: a
#: data-only or paper-only child has no business asking for execution. The
#: governed path still refuses an unapproved or ungranted request afterwards.
REQUEST_ACTION = "proposals:submit"


def _strategies_session_factory(request: Request):
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _attribution_store(request: Request):
    from backend.strategies.attribution import SqlAttributionStore

    store = getattr(request.app.state, "attribution_store", None)
    if store is None:
        store = SqlAttributionStore(session_factory=_strategies_session_factory(request))
    return store


def _next_action(row: Dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    if status == "awaiting_approval":
        return "waiting for the account owner to approve this plan"
    if status == "queued":
        return "queued for execution under the recorded authorization"
    if status == "dispatching":
        return "execution in progress"
    if status == "executed":
        outcome = str(dict(row.get("execution_detail") or {}).get("outcome_state") or "")
        if outcome == "filled":
            return "executed: every step is filled"
        return (
            "dispatched and submitted; fills, settlement and protection are tracked "
            "by the execution trail"
        )
    if status == "rejected":
        return "the account owner rejected this request"
    if status == "dispatch_unresolved":
        return (
            "the submission outcome is unknown and is not retried without durable "
            "evidence; ask the account owner to inspect it"
        )
    if status == "refused":
        return f"refused: {row.get('refusal_code') or 'unspecified'}"
    return "no action required"


def _request_response(row: Dict[str, Any], *, idempotent: bool = False) -> Dict[str, Any]:
    payload = {
        key: value
        for key, value in row.items()
        if key
        in {
            "request_id",
            "strategy_id",
            "strategy_run_id",
            "account_id",
            "execution_environment",
            "plan_id",
            "plan_hash",
            "authorization_mode",
            "grant_id",
            "status",
            "refusal_code",
            "refusal_detail",
            "decision_kind",
            "decision_at",
            "approval_id",
            "reservation_id",
            "execution_detail",
            "version_id",
            "attempt",
            "created_at",
            "updated_at",
            "terminal",
            "executable",
        }
    }
    #: ``outcome_state`` is the EXECUTOR's own word (submitted / filled /
    #: rejected / failed / uncertain / no_op). ``terminal`` means "this request
    #: will not be dispatched again" - never "the trade finished".
    # A UUID column read back through some drivers arrives as a UUID object, not
    # a string; the response contract is text, so the nullable ids are coerced
    # rather than left to fail validation with a 500.
    for key in ("grant_id", "approval_id", "reservation_id", "decision_kind", "refusal_code"):
        value = payload.get(key)
        payload[key] = None if value is None else str(value)
    payload["outcome_state"] = dict(row.get("execution_detail") or {}).get("outcome_state")
    payload["idempotent"] = bool(idempotent)
    payload["next_action"] = _next_action(row)
    return payload


async def request_execution(
    request: Request, payload: RunExecutionRequestCreate
) -> RunExecutionRequestResponse:
    """Ask the platform to execute one frozen plan under the strategy's mode."""
    token = await require_worker_token(request)
    _require_action(token, REQUEST_ACTION)
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    await enforce_hosted_attempt_authority(request, token, run)
    await require_active_worker_run_session(request, run)
    job = await hosted_job_for_run(request, run)
    if job is None:
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "HOSTED_EXECUTION_UNSUPPORTED",
                "strategy_run_id": payload.strategy_run_id,
                "message": (
                    "governed execution requests exist only for hosted strategies; "
                    "external workers keep their existing contract"
                ),
            },
        )
    service = ExecutionRequestService(_strategies_session_factory(request))
    try:
        result = service.create_for_job(
            job=job,
            plan_id=payload.plan_id,
            idempotency_key=payload.idempotency_key,
        )
    except ExecutionRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return RunExecutionRequestResponse(
        **_request_response(
            result["request"], idempotent=bool(result.get("idempotent"))
        )
    )


async def list_execution_requests(
    request: Request,
    strategy_run_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> RunExecutionRequestListResponse:
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    service = ExecutionRequestService(_strategies_session_factory(request))
    rows = service.list_for_run(strategy_run_id, limit=limit)
    return RunExecutionRequestListResponse(
        strategy_run_id=strategy_run_id,
        requests=[RunExecutionRequestResponse(**_request_response(row)) for row in rows],
    )


async def get_execution_request(
    request: Request,
    request_id: str,
    strategy_run_id: str = Query(min_length=1),
) -> RunExecutionRequestResponse:
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    service = ExecutionRequestService(_strategies_session_factory(request))
    row = service.get(request_id)
    if row is None or str(row.get("strategy_run_id") or "") != str(strategy_run_id):
        raise HTTPException(status_code=404, detail="Execution request not found")
    return RunExecutionRequestResponse(**_request_response(row))


async def get_owned_work(
    request: Request, strategy_run_id: str
) -> OwnedPositionsResponse:
    """The run's canonical strategy book plus its pending execution work.

    Reads are scoped to the run's own binding: the projection and the work are
    the STRATEGY's, so an earlier attempt's exposure and an in-flight request are
    both visible. An unpublished projection reports unknown coverage rather than
    a flat book.
    """
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    binding = _attribution_store(request).run_binding(strategy_run_id=str(strategy_run_id))
    if binding is None:
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "AUTHORITY_MISMATCH",
                "strategy_run_id": strategy_run_id,
                "message": "this run has no strategy binding, so it has no owned book",
            },
        )
    snapshot = OwnedWorkSnapshotService(_strategies_session_factory(request)).snapshot(
        strategy_id=str(binding["strategy_id"]),
        account_id=str(binding["account_id"]),
        execution_environment=str(binding["execution_environment"]),
        strategy_run_id=str(strategy_run_id),
    )
    return OwnedPositionsResponse(
        strategy_run_id=snapshot["strategy_run_id"],
        strategy_id=snapshot["strategy_id"],
        account_id=snapshot["account_id"],
        execution_environment=snapshot["execution_environment"],
        projection=ProjectionPublication(**snapshot["projection"]),
        positions=[OwnedPositionRow(**row) for row in snapshot["positions"]],
        pending=[PendingWorkRow(**row) for row in snapshot["pending"]],
        option_runs=[
            OwnedOptionRunRow(**row) for row in snapshot.get("option_runs") or []
        ],
        option_runs_coverage=OwnedOptionRunsCoverage(
            **(snapshot.get("option_runs_coverage") or {})
        ),
        coverage=snapshot["coverage"],
        observed_at=snapshot["observed_at"],
        notes=list(snapshot["notes"]),
    )


router.add_api_route(
    "/worker/executions",
    request_execution,
    methods=["POST"],
    response_model=RunExecutionRequestResponse,
    status_code=201,
)
router.add_api_route(
    "/worker/executions",
    list_execution_requests,
    methods=["GET"],
    response_model=RunExecutionRequestListResponse,
)
router.add_api_route(
    "/worker/executions/{request_id}",
    get_execution_request,
    methods=["GET"],
    response_model=RunExecutionRequestResponse,
)
router.add_api_route(
    "/worker/runs/{strategy_run_id}/positions",
    get_owned_work,
    methods=["GET"],
    response_model=OwnedPositionsResponse,
)
