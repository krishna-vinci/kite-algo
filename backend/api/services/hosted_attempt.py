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
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from backend.api.repositories.algo_worker_repo import WorkerToken
from backend.strategies import service as strategy_service
from backend.strategies.models import StrategyJob
from backend.strategies.repository import SqlAlchemyStrategyRepository

__all__ = [
    "HOSTED_TEMPLATE_PREFIX",
    "assert_child_lifecycle_forbidden",
    "assert_hosted_run_binding",
    "enforce_hosted_attempt_authority",
    "hosted_job_for_token",
    "is_hosted_run",
    "is_hosted_template_id",
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
    run: Dict[str, Any],
) -> None:
    run_id = str(run.get("strategy_run_id") or "")
    strategy_id = str(job.strategy_id or "")

    # The run must be the one this job recorded, and the token must be the child
    # credential minted for it. Neither the run id nor the token id alone grants
    # authority.
    if str(job.run_id or "") != run_id or str(job.token_id or "") != token.token_id:
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
