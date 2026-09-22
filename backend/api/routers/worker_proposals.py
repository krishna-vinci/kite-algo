"""Worker-facing proposal submission (G5).

Authorization ordering is identical to every other worker mutation route, and
deliberately so:

    token -> action scope -> run -> _assert_run_access -> hosted attempt ->
    session freshness -> authority checks -> work

The authority check is the G1 binding, and it **fails closed** (D-8). A run with
no binding cannot open authority at all — a legacy run is not a special case to
be papered over, it is a run whose attribution is unknown, and an unknown
attribution must never be able to create a plan. The payload's ``strategy_id``
and ``account_scope`` are claims to be confirmed against that binding, never
identity: a mismatch is refused ``AUTHORITY_MISMATCH`` rather than silently
rebinding the run to whatever the caller sent.

For a **hosted** run the persisted ``strategy_jobs`` record is the authority for
strategy, account and — when the job came from a schedule occurrence — the
bound evaluation id. The caller's ``job_id`` is a claim to confirm (never
identity), and an evaluation that does not match the job's binding is refused
``EVALUATION_IDENTITY_MISMATCH``.

What the pre-persistence fence actually enforces, exactly:

* ``enforce_hosted_attempt_authority`` is the freshness/authority check for a
  hosted attempt: the run must be the job's ``run_id``, the token must be that
  job's ``token_id``, the template must match, the pinned config must agree, and
  the attempt must be live (``starting``/``running``, ``desired_state=started``,
  unexpired lease). A fenced, stopped, unknown or expired attempt is refused
  here, before anything is written.
* ``require_active_worker_run_session`` enforces session **nonce equality** when
  the run has claimed one (``WORKER_SESSION_REQUIRED``/``WORKER_SESSION_CONFLICT``).
  A run that has claimed no nonce has nothing to compare and is a no-op, which is
  the established external-worker behavior; freshness for a hosted attempt is
  carried by the job's lease above, not by this header.

Submission creates a durable envelope and, on success, a frozen plan. It places
nothing: no admission, no reservation, no execution. Nothing in this phase reads
the plan back for trading.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from backend.api.services.hosted_attempt import (
    enforce_hosted_attempt_authority,
    hosted_job_for_run,
)
from backend.api.routers.worker_shared import (
    _assert_run_access,
    _require_action,
    _repo,
    require_active_worker_run_session,
    require_worker_token,
)
from backend.api.schemas.proposals import PlanResponse, ProposalSubmitRequest, ProposalSubmitResponse
from backend.strategies.proposals import ProposalConflict, ProposalStore, ProposalStoreError
from backend.strategies.proposals import ProposalSubmission

router = APIRouter(prefix="/algo-workers", tags=["Algo Workers"])


def _strategies_session_factory(request: Request):
    """The hosted-strategy session factory, as the owner routes resolve it.

    ``ProposalStore``'s own default import was broken (it named a ``SessionLocal``
    that does not exist), so the route must inject the factory explicitly.
    """
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _store(request: Request) -> ProposalStore:
    store = getattr(request.app.state, "proposal_store", None)
    if store is None:
        store = ProposalStore(session_factory=_strategies_session_factory(request))
        request.app.state.proposal_store = store
    return store


def _authority_for(
    request: Request,
    run: Dict[str, Any],
    payload: ProposalSubmitRequest,
    *,
    hosted_job: Any = None,
) -> Dict[str, str]:
    """The run's durable binding, or a refusal. Never payload metadata."""
    from backend.strategies.attribution import SqlAttributionStore

    store = getattr(request.app.state, "attribution_store", None)
    if store is None:
        # Same injected factory as the envelope store: two different databases
        # would make the binding that authorizes a submission unreadable to the
        # writer that persists it.
        store = SqlAttributionStore(session_factory=_strategies_session_factory(request))
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
    if hosted_job is not None:
        # The persisted job is the other half of a hosted run's authority. The
        # binding and the job must agree; if they do not, the run's provenance is
        # ambiguous and no plan may be created from it.
        if (
            str(hosted_job.strategy_id or "") != str(binding.get("strategy_id") or "")
            or str(hosted_job.account_scope or "") != str(binding.get("account_id") or "")
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "rejection_reason": "AUTHORITY_MISMATCH",
                    "message": "The run's binding and its hosted job disagree",
                    "strategy_run_id": payload.strategy_run_id,
                },
            )
    return {
        "strategy_id": str(binding["strategy_id"]),
        "account_id": str(binding["account_id"]),
    }


def _bound_job_id(payload: ProposalSubmitRequest, hosted_job: Any) -> Any:
    """The job id the envelope records: derived from authority, never the caller.

    External (non-hosted) submissions are unchanged: there is no persisted job,
    so the caller's ``job_id`` is the provenance it always was.
    """
    if hosted_job is None:
        return payload.job_id
    job_id = str(hosted_job.id or "")
    if payload.job_id and str(payload.job_id) != job_id:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "JOB_IDENTITY_MISMATCH",
                "message": "This run's job was created by the platform; the payload's job_id is not it",
                "strategy_run_id": payload.strategy_run_id,
                "bound_job_id": job_id,
            },
        )
    return job_id or None


def _bound_evaluation_id(hosted_job: Any) -> Any:
    if hosted_job is None:
        return None
    identity = dict(getattr(hosted_job, "identity_json", None) or {})
    value = str(identity.get("evaluation_id") or "").strip()
    return value or None


def _require_bound_evaluation(payload: ProposalSubmitRequest, bound_evaluation_id: str) -> None:
    """An occurrence-driven job may only be answered by its own evaluation."""
    if (
        str(payload.evaluation_id) != bound_evaluation_id
        or str(payload.evaluation_kind) != "scheduled_occurrence"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "EVALUATION_IDENTITY_MISMATCH",
                "message": "This run is bound to a scheduled occurrence evaluation",
                "strategy_run_id": payload.strategy_run_id,
                "bound_evaluation_id": bound_evaluation_id,
                "submitted_evaluation_id": str(payload.evaluation_id),
            },
        )


async def submit_proposal(request: Request, payload: ProposalSubmitRequest) -> ProposalSubmitResponse:
    token = await require_worker_token(request)
    _require_action(token, "proposals:submit")
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    # Hosted attempt fencing and session freshness come BEFORE any persistence:
    # a fenced, expired or superseded attempt must not be able to create an
    # envelope (or spend an evaluation identity) even with a valid-looking token.
    await enforce_hosted_attempt_authority(request, token, run)
    await require_active_worker_run_session(request, run)

    hosted_job = await hosted_job_for_run(request, run)
    authority = _authority_for(request, run, payload, hosted_job=hosted_job)
    job_id = _bound_job_id(payload, hosted_job)
    bound_evaluation_id = _bound_evaluation_id(hosted_job)
    if bound_evaluation_id is not None:
        _require_bound_evaluation(payload, bound_evaluation_id)

    try:
        result = _store(request).submit(
            ProposalSubmission(
                strategy_id=authority["strategy_id"],
                account_id=authority["account_id"],
                evaluation_id=payload.evaluation_id,
                evaluation_kind=payload.evaluation_kind,
                job_id=job_id,
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
