"""Hosted-attempt authority enforcement for child-facing worker mutations.

A **hosted** run is one whose ``template_id`` is namespaced as ``hosted:<id>``.
For those runs the run token is a *child* credential minted by the supervisor
lifecycle API, and the authoritative attempt state lives in ``strategy_jobs``
(``run_id`` / ``token_id`` / ``attempt`` / ``lease_owner`` / ``lease_epoch`` /
``lease_until`` / ``status``).

This module is the single place that turns "is this a hosted run?" into an
enforced decision on the worker HTTP boundary. It is applied to the mutation
routes a hosted child can reach. External worker runs (any other template) are
untouched: the guard returns immediately, preserving their established behavior.

It is deliberately **not** an SDK convention and **not** a token-composition
rule alone: the decision is made server-side from persisted records on every
relevant request. A stale, expired, fenced or mismatched attempt is refused even
if the request carries a syntactically valid child token.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException, Request

from backend.api.repositories.algo_worker_repo import WorkerToken
from backend.strategies import service as strategy_service
from backend.strategies.models import StrategyJob
from backend.strategies.repository import SqlAlchemyStrategyRepository

__all__ = [
    "HOSTED_TEMPLATE_PREFIX",
    "GOVERNED_EXECUTION_SURFACE",
    "OWNER_MANDATED_LIMIT_KEYS",
    "OWNER_MANDATED_POLICY_KEYS",
    "assert_hosted_discretionary_mutation_allowed",
    "assert_hosted_owner_policy_keys_untouched",
    "assert_hosted_risk_update_within_mandate",
    "assert_child_lifecycle_forbidden",
    "assert_hosted_run_binding",
    "enforce_hosted_attempt_authority",
    "enforce_hosted_read_authority",
    "hosted_job_for_run",
    "hosted_job_for_token",
    "hosted_owner_for_token",
    "is_hosted_run",
    "is_hosted_template_id",
    "record_hosted_progress",
    "token_is_hosted_candidate",
]

HOSTED_TEMPLATE_PREFIX = "hosted:"

#: A hosted child attempt is authoritative only while the job is live.
_LIVE_ATTEMPT_STATUSES = ("starting", "running")


def is_hosted_template_id(template_id: Any) -> bool:
    return str(template_id or "").startswith(HOSTED_TEMPLATE_PREFIX)


def is_hosted_run(run: Optional[Dict[str, Any]]) -> bool:
    return bool(run) and is_hosted_template_id(run.get("template_id"))


def token_is_hosted_candidate(token: WorkerToken) -> bool:
    """Cheap, server-side signal that a token *may* be a hosted child token.

    A hosted child token is minted with its ``allowed_templates`` set to exactly
    ``[hosted:<strategy_id>]``. This is only a pre-filter to avoid touching the
    strategies store for ordinary external tokens; the authoritative decision is
    always the persisted ``strategy_jobs`` record (see ``hosted_job_for_token``).
    """
    return any(is_hosted_template_id(t) for t in (getattr(token, "allowed_templates", None) or []))


def _strategies_repo(request: Request) -> SqlAlchemyStrategyRepository:
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is None:
        from backend.app.database import SessionLocal

        factory = SessionLocal
    return SqlAlchemyStrategyRepository(factory)


def _reject(status_code: int, reason: str, **extra: Any) -> HTTPException:
    detail: Dict[str, Any] = {"rejection_reason": reason}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_hosted_job(repo: SqlAlchemyStrategyRepository, run_id: str) -> Optional[StrategyJob]:
    return repo.get_job_by_run_id(run_id)


def _validate_authority(
    job: StrategyJob,
    token: WorkerToken,
    run: Optional[Dict[str, Any]],
) -> None:
    """Validate the persisted hosted attempt for this token.

    ``run`` is the worker run record when the route is run-scoped. Read routes
    are not run-scoped (a quote or a universe read names no run), so they pass
    ``None`` and the token/attempt binding is still fully checked against the
    persisted job.
    """
    run = run or {}
    run_id = str(run.get("strategy_run_id") or "")
    strategy_id = str(job.strategy_id or "")

    # The token must be the child credential minted for this attempt. Neither a
    # run id nor a token id alone grants authority.
    if str(job.token_id or "") != token.token_id:
        raise _reject(
            403,
            "HOSTED_ATTEMPT_TOKEN_MISMATCH",
            strategy_run_id=run_id,
        )
    if run:
        # The run must be the one this job recorded.
        if str(job.run_id or "") != run_id:
            raise _reject(
                403,
                "HOSTED_ATTEMPT_TOKEN_MISMATCH",
                strategy_run_id=run_id,
            )
        if str(run.get("template_id") or "") != strategy_service.template_id_for(strategy_id):
            raise _reject(
                403,
                "HOSTED_ATTEMPT_TEMPLATE_MISMATCH",
                strategy_run_id=run_id,
            )

        # Configuration is derived from the persisted job, never from the request.
        if (
            str(job.execution_mode or "") != str(run.get("execution_mode") or "")
            or str(job.account_scope or "") != str(run.get("account_scope") or "")
        ):
            raise _reject(
                409,
                "HOSTED_ATTEMPT_CONFIG_MISMATCH",
                strategy_run_id=run_id,
            )

    if str(job.desired_state or "") != "started":
        raise _reject(
            409,
            "HOSTED_ATTEMPT_STOPPED",
            strategy_run_id=run_id,
        )

    status = str(job.status or "")
    if status not in _LIVE_ATTEMPT_STATUSES:
        # recovery_required / stopped / failed / hung / fencing / queued: an
        # attempt that is fenced or not yet authorized must not mutate state.
        raise _reject(
            409,
            "HOSTED_ATTEMPT_FENCED",
            strategy_run_id=run_id,
            job_status=status,
        )

    lease_until = job.lease_until
    if lease_until is None or _as_utc(lease_until) <= _now():
        raise _reject(
            409,
            "HOSTED_LEASE_EXPIRED",
            strategy_run_id=run_id,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _enforce_sync(request: Request, token: WorkerToken, run: Dict[str, Any]) -> None:
    if not is_hosted_run(run):
        return
    repo = _strategies_repo(request)
    job = _load_hosted_job(repo, str(run.get("strategy_run_id") or ""))
    if job is None:
        raise _reject(
            403,
            "HOSTED_ATTEMPT_UNKNOWN",
            strategy_run_id=str(run.get("strategy_run_id") or ""),
        )
    _validate_authority(job, token, run)


def _enforce_read_sync(request: Request, token: WorkerToken) -> Optional[StrategyJob]:
    repo = _strategies_repo(request)
    job = repo.get_job_by_token_id(token.token_id)
    if job is None:
        # A hosted-shaped token with no persisted attempt is not a live
        # credential: fail closed rather than granting the read.
        raise _reject(403, "HOSTED_ATTEMPT_UNKNOWN")
    _validate_authority(job, token, None)
    return job


async def enforce_hosted_attempt_authority(
    request: Request, token: WorkerToken, run: Optional[Dict[str, Any]]
) -> None:
    """Refuse a hosted child mutation with stale/expired/fenced authority.

    No-op for an external (non-hosted) run. Raises 403/409 for a hosted run whose
    persisted attempt authority does not authorize the request.
    """
    if not is_hosted_run(run):
        return
    await asyncio.to_thread(_enforce_sync, request, token, run)


async def enforce_hosted_read_authority(
    request: Request, token: WorkerToken
) -> Optional[StrategyJob]:
    """Refuse a hosted child **read** whose persisted attempt is no longer live.

    A hosted child token is bound to a ``strategy_jobs`` row. Reads are not
    run-scoped, so this checks the persisted attempt itself: the token must be
    the one minted for the job, ``desired_state`` must still be ``started``,
    the attempt must not be fenced/stopped/failed/hung, and its lease must not
    have expired. Raises 403 for an unknown/fenced attempt and 409 for a
    stopped or expired one.

    External (non-hosted) tokens are untouched: the cheap
    ``token_is_hosted_candidate`` pre-filter returns ``None`` without touching
    the strategies store, preserving the established external contract.
    """
    if not token_is_hosted_candidate(token):
        return None
    return await asyncio.to_thread(_enforce_read_sync, request, token)


async def hosted_owner_for_token(request: Request, token: WorkerToken) -> Optional[str]:
    """The **application** owner of a hosted strategy, or ``None``.

    The owner is derived from the persisted ``strategy_jobs``/strategy record
    (``app:<username>``), never from the token's ``account_scope`` — the broker
    account scope is a trading-account selection and is not owner identity.
    """
    job = await hosted_job_for_token(request, token)
    if job is None:
        return None
    owner = str(getattr(job, "owner_id", "") or "").strip()
    return owner or None


def assert_child_lifecycle_forbidden(run: Optional[Dict[str, Any]], operation: str) -> None:
    """A hosted child token may never claim, heartbeat or release its session.

    Those are runner-owned lifecycle operations (``{operation}`` names which one)
    and are performed only by the supervisor through the lifecycle API. This is a
    hard refusal, not a convention: even a child token that somehow carried the
    ``heartbeat`` action would be rejected here.
    """
    if not is_hosted_run(run):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "rejection_reason": "HOSTED_CHILD_LIFECYCLE_FORBIDDEN",
            "operation": operation,
            "strategy_run_id": str(run.get("strategy_run_id") or ""),
        },
    )


#: The governed surface a hosted child must use for discretionary exposure.
GOVERNED_EXECUTION_SURFACE = "/api/algo-workers/worker/executions"

#: The recorded limit keys a hosted child's risk patch may not raise. They are
#: the owner-mandated ceilings the platform already enforces at admission, so a
#: child-editable risk blob must never be able to move them upward.
OWNER_MANDATED_LIMIT_KEYS = (
    "allocation_inr",
    "per_instrument_notional_inr",
    "gross_notional_inr",
    "max_open_instruments",
    "admissions_per_window",
    "admission_window_seconds",
    "daily_loss_budget_inr",
)

#: The owner-recorded capital/risk policy keys. A hosted child may not write ANY
#: of them, in either direction: the recorded policy is an owner surface, and
#: even a tightening is an owner decision (it invalidates the standing grant).
OWNER_MANDATED_POLICY_KEYS = frozenset(OWNER_MANDATED_LIMIT_KEYS)


def assert_hosted_owner_policy_keys_untouched(
    run: Optional[Dict[str, Any]],
    payload: Optional[Mapping[str, Any]],
    *,
    operation: str,
) -> None:
    """A hosted child may not write an owner-mandated policy key at all.

    An earlier numeric comparison was too clever and too weak: an unreadable
    mandate let the patch through, only one ceiling direction was compared, and
    ``None``/NaN values were silently ignored. The owner's recorded capital/risk
    policy is an OWNER surface, so for a hosted child any payload that names one
    of those keys is refused by name. Per-run behaviour keys are unaffected, and
    external worker runs are untouched.
    """
    if not is_hosted_run(run):
        return
    named = sorted(
        {
            str(key)
            for key in dict(payload or {}).keys()
            if str(key) in OWNER_MANDATED_POLICY_KEYS
        }
    )
    if not named:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "rejection_reason": "HOSTED_OWNER_POLICY_MUTATION_FORBIDDEN",
            "operation": str(operation),
            "strategy_run_id": str(run.get("strategy_run_id") or ""),
            "owner_mandated_keys": named,
            "message": (
                "the strategy's capital/risk policy is owner-recorded; a hosted child "
                "cannot write these keys. Change the admission policy through the owner "
                "surface instead."
            ),
        },
    )


async def assert_hosted_risk_update_within_mandate(
    request: Request, run: Optional[Dict[str, Any]], patch: Optional[Dict[str, Any]]
) -> None:
    """A hosted child's risk patch may not write an owner-mandated policy key.

    The mandated capital/risk keys are an OWNER surface, so the check refuses any
    patch that names one (rather than trying to decide whether one direction of
    one key is a "relaxation"). An unreadable, absent, ``None`` or NaN mandate is
    therefore irrelevant: the key list is a static vocabulary, not a read.
    """
    _ = request  # kept in the signature for the route's existing call shape
    assert_hosted_owner_policy_keys_untouched(run, patch, operation="risk:update")


def assert_hosted_discretionary_mutation_allowed(
    run: Optional[Dict[str, Any]], *, operation: str
) -> None:
    """A hosted child may not make discretionary raw exposure-changing calls.

    Phase 2 closed a real bypass: a hosted child holding ``intents:submit`` (or
    ``runs:exit``) could place, modify, bracket or flatten orders directly,
    entirely outside the approval/autonomous decision the owner chose. Those
    calls now go through the governed execution-request contract
    (``POST /api/algo-workers/worker/executions``), or they are refused by name.

    External worker runs are untouched: this returns immediately for them, and
    platform-authorised risk reduction does not travel through a child token.
    """
    if not is_hosted_run(run):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "rejection_reason": "HOSTED_RAW_MUTATION_FORBIDDEN",
            "operation": str(operation),
            "strategy_run_id": str(run.get("strategy_run_id") or ""),
            "governed_surface": GOVERNED_EXECUTION_SURFACE,
            "message": (
                "hosted discretionary order mutation is not available on the raw worker "
                "surface: submit the change as a proposal and request execution under the "
                "strategy's authorisation mode"
            ),
        },
    )


async def hosted_job_for_run(request: Request, run: Optional[Dict[str, Any]]) -> Optional[StrategyJob]:
    """The persisted hosted job for a hosted run, or ``None`` for external runs."""
    if not is_hosted_run(run):
        return None
    repo = _strategies_repo(request)
    return await asyncio.to_thread(repo.get_job_by_run_id, str((run or {}).get("strategy_run_id") or ""))


async def hosted_job_for_token(request: Request, token: WorkerToken) -> Optional[StrategyJob]:
    """The persisted hosted job bound to this token, or ``None``.

    Returns ``None`` for an ordinary external token *without touching the
    strategies store* (the cheap ``token_is_hosted_candidate`` pre-filter). For a
    candidate, the authoritative answer is the ``strategy_jobs`` record bound to
    the token id.
    """
    if not token_is_hosted_candidate(token):
        return None
    repo = _strategies_repo(request)
    return await asyncio.to_thread(repo.get_job_by_token_id, token.token_id)


def assert_hosted_run_binding(run: Optional[Dict[str, Any]], token: WorkerToken) -> None:
    """A hosted child token may only act on the worker run bound to its attempt.

    Routes that do not otherwise require a worker run (the options surface) must
    call this when the caller is hosted: a hosted credential may not select an
    options id with no corresponding worker run, nor a run owned by another
    token. Fails closed with 403.
    """
    if run is None or str(run.get("token_id") or "") != token.token_id:
        raise _reject(
            403,
            "HOSTED_CHILD_RUN_REQUIRED",
            strategy_run_id=str((run or {}).get("strategy_run_id") or ""),
        )


async def record_hosted_progress(
    request: Request, token: WorkerToken, run: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Record a child-accepted progress marker on the hosted attempt.

    Hosted-only: an external (non-``hosted:``) run has no hosted attempt to
    attribute progress to, so it is refused rather than inventing semantics.
    The full attempt authority is re-validated (fenced/expired/mismatched are
    refused) before ``last_progress_at`` is written.
    """
    if not is_hosted_run(run):
        raise _reject(403, "HOSTED_PROGRESS_UNSUPPORTED")
    run_id = str((run or {}).get("strategy_run_id") or "")
    repo = _strategies_repo(request)
    job = await asyncio.to_thread(_load_hosted_job, repo, run_id)
    if job is None:
        raise _reject(403, "HOSTED_ATTEMPT_UNKNOWN", strategy_run_id=run_id)
    await asyncio.to_thread(_validate_authority, job, token, run)
    updated = await asyncio.to_thread(repo.record_progress, job.id)
    return {"job_id": job.id, "strategy_run_id": run_id, "updated": bool(updated)}
