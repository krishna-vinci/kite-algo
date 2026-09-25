"""Owner actions for one hosted strategy (B2.6b S1 and S2).

Five routes, three of them mutations:

* ``GET  /api/strategies/{strategy_id}/owner-actions/pending-work`` - the
  strategy's own pending ENTRY work, with a stable ``evidence_digest`` and a
  per-candidate eligibility verdict;
* ``POST /api/strategies/{strategy_id}/owner-actions/cancel-pending`` - cancel
  only what that preview proved eligible;
* ``GET  /api/strategies/{strategy_id}/plans/{plan_id}/steps/{step_no}/dead-submission``
  - the platform's own evidence about ONE unanswered plan step, and which
  terminal dispositions it supports;
* ``POST .../dead-submission`` - apply one of those dispositions.
* ``GET/POST /api/strategies/{strategy_id}/option-runs/{option_run_id}/exit`` -
  the owner-authorized discretionary exit of ONE option run (S2), which submits
  one stage of the STAGED structure exit derived from the run's own confirmed
  fills.

Authorization is uniform: the owner comes from ``require_strategy_owner`` (never
a caller value), the account/environment scope is derived server-side exactly
like the governed option-run repair path, and every POST enforces same-origin.
What each action may do lives in ``backend.api.services.owner_actions``; this
module is the HTTP mapping and nothing else.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.routers.strategies import require_strategy_owner
from backend.api.schemas.strategy_owner_actions import (
    CancelPendingRequest,
    DeadSubmissionDispositionRequest,
    DeadSubmissionResponse,
    OwnerActionItemResponse,
    OwnerActionResponse,
    OptionRunExitActionResponse,
    OptionRunExitItemResponse,
    OptionRunExitRequest,
    OptionRunExitResponse,
    PendingWorkItemResponse,
    PendingWorkResponse,
)
from backend.api.services.csrf import enforce_same_origin
from backend.api.services.hosted_strategy_authz import authorize_account_scope
from backend.api.services.owner_actions import (
    OwnerActionRefusal,
    OwnerActionsService,
    live_broker_cancel,
    owner_action_scope,
)
from backend.api.services.option_run_repair import (
    build_option_run_repair_service,
    option_run_repair_scope,
    owner_exit_gates,
    owner_exit_refusal,
    owner_exit_stage_items,
    owner_exit_submission_refusal,
    owner_exit_view,
    record_owner_exit_audit,
    require_owner_exit_boundary,
    submit_owner_exit_stage,
)
from backend.options.execution.repair import (
    ACTION_OWNER_EXIT,
    STATE_FLAT,
    STATE_RESIDUAL,
    TERMINAL_RUN_STATUSES,
    OptionRunRepairRefusal,
)
from backend.strategies.repository import SqlAlchemyStrategyRepository

router = APIRouter(prefix="/strategies", tags=["Hosted strategies (operator)"])

logger = logging.getLogger(__name__)

__all__ = ["router"]


def _owner_actions_db(request: Request):
    """Sessionmaker for the hosted-strategy tables (injectable for tests)."""
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _repository(
    request: Request, session_factory: Any = Depends(_owner_actions_db)
) -> SqlAlchemyStrategyRepository:
    return SqlAlchemyStrategyRepository(session_factory)


def _service(
    request: Request, session_factory: Any = Depends(_owner_actions_db)
) -> OwnerActionsService:
    """The action service over the app's own durable stores.

    The paper runtime and the option-run store are whatever the app wired; a
    deployment that has none refuses by name rather than inventing a boundary.
    """
    state = getattr(getattr(request, "app", None), "state", None)
    return OwnerActionsService(
        session_factory=session_factory,
        run_store=getattr(state, "option_run_store", None),
        paper_service=getattr(state, "paper_runtime_service", None),
        repository=SqlAlchemyStrategyRepository(session_factory),
        #: The existing fake-testable broker boundary. Its absence makes a live
        #: cancel UNKNOWN, which is reported as blocked - never assumed.
        broker_cancel=getattr(state, "owner_action_broker_cancel", None)
        or live_broker_cancel,
    )


def _plan_for(
    request: Request,
    *,
    owner: str,
    repo: SqlAlchemyStrategyRepository,
    strategy_id: str,
    plan_id: str,
    session_factory: Any,
):
    """The plan, owner-scoped and account-authorized, or a 404.

    Foreign and missing are indistinguishable on purpose, and the account comes
    from the plan itself - the caller never names it.
    """
    if repo.get_strategy(owner, strategy_id) is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    from backend.strategies.proposals import ProposalStore

    store = getattr(request.app.state, "proposal_store", None)
    if store is None:
        store = ProposalStore(session_factory=session_factory)
    plan = store.get_plan(plan_id)
    if plan is None or str(plan["strategy_id"]) != str(strategy_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    authorize_account_scope(str(plan["account_id"]))
    return plan


def _repair_service(
    request: Request, session_factory: Any = Depends(_owner_actions_db)
):
    """The repair machinery in its owner-exit view.

    One engine, not two: the exit reads the run through the SAME durable store,
    the SAME ``StagedStructureExit`` and the SAME run CAS the governed repair
    close does.
    """
    return build_option_run_repair_service(request, session_factory)


def _item(row: Any) -> OwnerActionItemResponse:
    return OwnerActionItemResponse(
        plan_id=str(row.get("plan_id") or ""),
        step_no=int(row.get("step_no") or 0),
        order_id=row.get("order_id"),
        eligibility=str(row.get("eligibility") or "eligible"),
        outcome=str(row.get("outcome") or ""),
        filled_quantity=int(row.get("filled_quantity") or 0),
        remaining_quantity=int(row.get("remaining_quantity") or 0),
        disposition=row.get("disposition"),
        run_status=row.get("run_status"),
        reason_code=row.get("reason_code"),
    )


@router.get(
    "/{strategy_id}/owner-actions/pending-work", response_model=PendingWorkResponse
)
async def preview_pending_work(
    strategy_id: str,
    request: Request,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_owner_actions_db),
    service: OwnerActionsService = Depends(_service),
):
    """The strategy's own pending entry work, and what may be cancelled.

    Nothing is moved here. The digest the response carries is what a POST must
    still match, so a fill or a terminal outcome between the two invalidates the
    action instead of being raced.
    """
    _ = request
    scope = owner_action_scope(repo, owner, strategy_id, environment)
    preview = service.preview_pending(scope)
    return PendingWorkResponse(
        coverage=str(preview.get("coverage") or "unknown"),
        evidence_digest=str(preview.get("evidence_digest") or ""),
        items=[
            PendingWorkItemResponse(
                plan_id=str(row.get("plan_id") or ""),
                step_no=int(row.get("step_no") or 0),
                order_id=row.get("order_id"),
                remaining_quantity=int(row.get("remaining_quantity") or 0),
                eligibility=str(row.get("eligibility") or "ineligible"),
                reason_code=row.get("reason_code"),
            )
            for row in (preview.get("items") or [])
        ],
    )


@router.post(
    "/{strategy_id}/owner-actions/cancel-pending", response_model=OwnerActionResponse
)
async def cancel_pending_work(
    strategy_id: str,
    request: Request,
    payload: CancelPendingRequest,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_owner_actions_db),
    service: OwnerActionsService = Depends(_service),
):
    """Cancel qualifying pending entry work, or refuse by name.

    Only the candidates the preview proved eligible are cancelled: a protective
    hedge, a reduction, an unowned order or an unreadable basis is reported as
    ``skipped`` with its named reason and is never touched.
    """
    enforce_same_origin(request)
    scope = owner_action_scope(repo, owner, strategy_id, environment)
    try:
        result = await service.cancel_pending(
            scope,
            evidence_digest=str(payload.evidence_digest or ""),
            reason=str(payload.reason or ""),
            actor=str(owner),
        )
    except OwnerActionRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return OwnerActionResponse(
        status=str(result.get("status") or "blocked"),
        action_id=str(result.get("action_id") or ""),
        evidence_digest=str(result.get("evidence_digest") or ""),
        items=[_item(row) for row in (result.get("items") or [])],
        refusal=result.get("refusal"),
        audit_id=result.get("audit_id"),
    )


@router.get(
    "/{strategy_id}/option-runs/{option_run_id}/exit",
    response_model=OptionRunExitResponse,
)
async def inspect_option_run_exit(
    strategy_id: str,
    option_run_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_owner_actions_db),
    service: Any = Depends(_repair_service),
):
    """The staged structure exit of ONE option run, and what it would submit.

    Nothing is moved: the verdict is derived from the run's own confirmed fills
    (shorts first, a hedge only once its short is proven closed) and the returned
    ``evidence_digest`` is what a POST must still match.
    """
    _ = request
    option_run_repair_scope(repo, owner, strategy_id, option_run_id, session_factory)
    try:
        view = owner_exit_view(service, option_run_id)
    except OptionRunRepairRefusal as exc:
        mapped = owner_exit_refusal(exc)
        raise HTTPException(
            status_code=mapped.status_code, detail=mapped.as_detail()
        ) from exc
    return OptionRunExitResponse(**view)


@router.post(
    "/{strategy_id}/option-runs/{option_run_id}/exit",
    response_model=OptionRunExitActionResponse,
)
async def exit_option_run(
    strategy_id: str,
    option_run_id: str,
    request: Request,
    payload: OptionRunExitRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_owner_actions_db),
    service: Any = Depends(_repair_service),
):
    """Owner-authorized discretionary exit of ONE option run.

    The server alone decides what the run holds, and the run's own transition is
    the ownership token: exactly one caller takes ``entered`` / ``exiting`` and
    submits ONE stage of the derived close plan. Nothing is submitted when a gate
    refuses, and the run is never marked ``exited`` merely because a broker
    accepted a stage - completion is the run's own fills proving it flat.
    """
    enforce_same_origin(request)
    scope = option_run_repair_scope(repo, owner, strategy_id, option_run_id, session_factory)
    # The specific gates are asked on a fresh read BEFORE the transition: an
    # unresolved stage or an unfinished adjust outranks "your evidence is stale"
    # as the explanation, and nothing has moved yet either way.
    try:
        owner_exit_gates(service.assessment(option_run_id, owner_exit=True))
    except OptionRunRepairRefusal as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.as_detail()
        ) from exc
    try:
        next_run, assessment = service.plan(
            option_run_id=option_run_id,
            action=ACTION_OWNER_EXIT,
            evidence_digest=str(payload.evidence_digest or ""),
            owner_exit=True,
        )
    except OptionRunRepairRefusal as exc:
        mapped = owner_exit_refusal(exc)
        raise HTTPException(
            status_code=mapped.status_code, detail=mapped.as_detail()
        ) from exc
    state = str(assessment.get("state") or "")
    observed_status = str(assessment.get("status") or "")
    # Everything that can refuse happens BEFORE the run moves: an unavailable
    # live boundary must never leave a claimed stage that nothing can send.
    boundary = None
    if state == STATE_RESIDUAL:
        boundary = await require_owner_exit_boundary(request, scope=scope, run=next_run)
    if state == STATE_FLAT and observed_status in TERMINAL_RUN_STATUSES:
        # Already past the exit: report it complete, do not write the same
        # terminal status again.
        committed = next_run
    else:
        try:
            committed = service.commit(next_run, allowed_from=observed_status)
        except OptionRunRepairRefusal as exc:
            mapped = owner_exit_refusal(exc, status=observed_status)
            raise HTTPException(
                status_code=mapped.status_code, detail=mapped.as_detail()
            ) from exc
    action_id = str(uuid.uuid4())
    submission: Any = {}
    if boundary is not None:
        submission = await submit_owner_exit_stage(
            request, session_factory, run=committed, scope=scope, boundary=boundary
        )
    refusal = None if state == STATE_FLAT else owner_exit_submission_refusal(submission)
    if state == STATE_FLAT:
        status = "complete"
    elif refusal is None and submission.get("submitted"):
        status = "accepted"
    else:
        status = "blocked"
    audit_id = record_owner_exit_audit(
        session_factory,
        repo,
        strategy_id=str(strategy_id),
        run=committed,
        action_id=action_id,
        assessment=assessment,
        submission=submission,
        reason=str(payload.reason or ""),
        actor=str(owner),
    )
    return OptionRunExitActionResponse(
        status=status,
        action_id=action_id,
        option_run_id=str(committed.strategy_run_id),
        run_status=str(committed.status),
        state=state,
        evidence_digest=str(assessment.get("evidence_digest") or ""),
        items=[OptionRunExitItemResponse(**row) for row in owner_exit_stage_items(submission)],
        refusal=refusal,
        audit_id=audit_id,
        submission=dict(submission or {}),
    )


@router.get(
    "/{strategy_id}/plans/{plan_id}/steps/{step_no}/dead-submission",
    response_model=DeadSubmissionResponse,
)
async def inspect_dead_submission(
    strategy_id: str,
    plan_id: str,
    step_no: int,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_owner_actions_db),
    service: OwnerActionsService = Depends(_service),
):
    """The evidence for ONE unanswered plan step, and its own disposition list.

    Evidence comes from the paper order / progress row or the broker order
    projection; the owner cannot type an outcome into existence. A step whose
    send is unknown, whose remainder is still open, or which belongs to a staged
    protective exit refuses by name.
    """
    plan = _plan_for(
        request,
        owner=owner,
        repo=repo,
        strategy_id=strategy_id,
        plan_id=plan_id,
        session_factory=session_factory,
    )
    scope = owner_action_scope(repo, owner, strategy_id)
    scope["account_id"] = str(plan["account_id"])
    try:
        evidence = service.dead_submission(scope, plan=plan, step_no=int(step_no))
    except OwnerActionRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    # The service's evidence carries internal verdicts (``terminal`` /
    # ``open_remainder``); the response is the §5 contract and nothing else.
    return DeadSubmissionResponse(
        plan_id=str(evidence.get("plan_id") or ""),
        step_no=int(evidence.get("step_no") or 0),
        execution_environment=str(evidence.get("execution_environment") or ""),
        trail_state=str(evidence.get("trail_state") or ""),
        source=str(evidence.get("source") or ""),
        status=str(evidence.get("status") or ""),
        order_id=evidence.get("order_id"),
        requested_quantity=evidence.get("requested_quantity"),
        filled_quantity=int(evidence.get("filled_quantity") or 0),
        remaining_quantity=int(evidence.get("remaining_quantity") or 0),
        allowed_dispositions=[
            str(value) for value in (evidence.get("allowed_dispositions") or [])
        ],
        evidence_digest=str(evidence.get("evidence_digest") or ""),
    )


@router.post(
    "/{strategy_id}/plans/{plan_id}/steps/{step_no}/dead-submission",
    response_model=OwnerActionResponse,
)
async def dispose_dead_submission(
    strategy_id: str,
    plan_id: str,
    step_no: int,
    request: Request,
    payload: DeadSubmissionDispositionRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_owner_actions_db),
    service: OwnerActionsService = Depends(_service),
):
    """Apply ONE evidence-backed disposition, or refuse by name.

    On success the terminal trail event and the barrier's ``work_resolved`` are
    written in ONE transaction, so the plan's own execution state reports
    ``finished`` afterwards and the adjust takeover / repair gates can proceed.
    """
    enforce_same_origin(request)
    plan = _plan_for(
        request,
        owner=owner,
        repo=repo,
        strategy_id=strategy_id,
        plan_id=plan_id,
        session_factory=session_factory,
    )
    scope = owner_action_scope(repo, owner, strategy_id)
    scope["account_id"] = str(plan["account_id"])
    try:
        result = service.dispose_dead_submission(
            scope,
            plan=plan,
            step_no=int(step_no),
            evidence_digest=str(payload.evidence_digest or ""),
            disposition=str(payload.disposition or ""),
            reason=str(payload.reason or ""),
            actor=str(owner),
        )
    except OwnerActionRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return OwnerActionResponse(
        status=str(result.get("status") or "blocked"),
        action_id=str(result.get("action_id") or ""),
        evidence_digest=str(result.get("evidence_digest") or ""),
        items=[_item(row) for row in (result.get("items") or [])],
        refusal=result.get("refusal"),
        audit_id=result.get("audit_id"),
    )
