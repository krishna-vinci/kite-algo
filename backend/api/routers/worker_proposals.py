"""Worker-facing proposal submission (G5).

Authorization ordering is identical to every other worker mutation route, and
deliberately so:

    token -> action scope -> run -> _assert_run_access -> authority checks -> work

The authority check is the G1 binding, and it **fails closed** (D-8). A run with
no binding cannot open authority at all — a legacy run is not a special case to
be papered over, it is a run whose attribution is unknown, and an unknown
attribution must never be able to create a plan. The payload's ``strategy_id``
and ``account_scope`` are claims to be confirmed against that binding, never
identity: a mismatch is refused ``AUTHORITY_MISMATCH`` rather than silently
rebinding the run to whatever the caller sent.

Submission creates a durable envelope and, on success, a frozen plan. It places
nothing: no admission, no reservation, no execution. Nothing in this phase reads
the plan back for trading.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from backend.api.routers.worker_shared import (
    _assert_run_access,
    _require_action,
    _repo,
    require_worker_token,
)
from backend.api.schemas.proposals import PlanResponse, ProposalSubmitRequest, ProposalSubmitResponse
from backend.strategies.proposals import ProposalConflict, ProposalStore, ProposalStoreError
from backend.strategies.proposals import ProposalSubmission

router = APIRouter(prefix="/algo-workers", tags=["Algo Workers"])


def _store(request: Request) -> ProposalStore:
    store = getattr(request.app.state, "proposal_store", None)
    if store is None:
        store = ProposalStore()
        request.app.state.proposal_store = store
    return store


def _authority_for(request: Request, run: Dict[str, Any], payload: ProposalSubmitRequest) -> Dict[str, str]:
    """The run's durable binding, or a refusal. Never payload metadata."""
    from backend.strategies.attribution import SqlAttributionStore

    store = getattr(request.app.state, "attribution_store", None) or SqlAttributionStore()
    binding = store.run_binding(strategy_run_id=payload.strategy_run_id)
    if binding is None:
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "AUTHORITY_MISMATCH",
                "message": (
                    "This run has no strategy binding, so it cannot open proposal authority."
                ),
                "strategy_run_id": payload.strategy_run_id,
            },
        )
    if (
        str(binding.get("strategy_id") or "") != payload.strategy_id
        or str(binding.get("account_id") or "") != payload.account_scope
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "AUTHORITY_MISMATCH",
                "strategy_run_id": payload.strategy_run_id,
                "bound_strategy_id": str(binding.get("strategy_id") or ""),
                "bound_account_id": str(binding.get("account_id") or ""),
            },
        )
    if str(binding.get("account_id") or "") != str(run.get("account_scope") or ""):
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "AUTHORITY_MISMATCH",
                "message": "The run's account scope and its strategy binding disagree",
            },
        )
    return {
        "strategy_id": str(binding["strategy_id"]),
        "account_id": str(binding["account_id"]),
    }


async def submit_proposal(request: Request, payload: ProposalSubmitRequest) -> ProposalSubmitResponse:
    token = await require_worker_token(request)
    _require_action(token, "proposals:submit")
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    authority = _authority_for(request, run, payload)

    try:
        result = _store(request).submit(
            ProposalSubmission(
                strategy_id=authority["strategy_id"],
                account_id=authority["account_id"],
                evaluation_id=payload.evaluation_id,
                evaluation_kind=payload.evaluation_kind,
                job_id=payload.job_id,
                strategy_run_id=payload.strategy_run_id,
                target_kind=payload.target_kind,
                payload=dict(payload.payload),
            )
        )
    except ProposalConflict as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except ProposalStoreError as exc:
        raise HTTPException(
            status_code=422,
            detail={"rejection_reason": "PROPOSAL_INVALID", "message": str(exc)},
        ) from exc

    return ProposalSubmitResponse(
        proposal_id=result["proposal_id"],
        status=result["status"],
        plan=PlanResponse(**result["plan"]) if result.get("plan") else None,
        idempotent=bool(result.get("idempotent")),
    )


router.add_api_route(
    "/worker/proposals",
    submit_proposal,
    methods=["POST"],
    response_model=ProposalSubmitResponse,
    status_code=201,
)
