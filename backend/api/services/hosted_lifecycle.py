"""Hosted-strategy supervisor lifecycle service (launch preparation).

This is the server-side orchestration behind the credential-authenticated
lifecycle API. It performs *preparation only* — it never spawns or executes
strategy code. The exact ordering is:

    claim authorized job                        (done by the caller, CAS)
      → reserve + create child token            (hash stored; plaintext returned once)
      → create worker run bound to that token   (existing shared run-creation path)
      → claim the worker session                (runner-owned nonce)
      → deliver the child configuration         (one-time handoff, durable marker)

**Why the markers exist.** A worker token is stored only as a hash, so its
plaintext can never be recovered. Preparation therefore records, durably and in
order, ``token_id`` then ``run_id`` then ``handoff_at``:

- ``token_id`` is CAS-reserved *before* the token is minted, so a crash between
  the two leaves a visible, revocable marker instead of an invisible credential.
- A repeat that finds ``token_id`` set (with or without ``run_id``/
  ``handoff_at``) **fails closed**: it revokes the child authority, fences the
  job to ``recovery_required`` and requires reconciliation. It never mints a
  second credential for the same attempt and never pretends the plaintext is
  recoverable.
- Because both a delivered handoff and an uncertain one fail closed, the
  protocol is deliberately **not** exactly-once across a lost response.

Every mutating step is fenced by id + ``lease_owner`` + ``lease_epoch`` +
``attempt``, so a stale supervisor cannot act. No supervisor credential is ever
returned: the response carries the *child* token (for the supervisor to place in
the child's environment) and the run/session identifiers, nothing more.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository, WorkerToken
from backend.api.routers.worker_shared import (
    WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
    WORKER_SESSION_FRESHNESS_SECONDS,
    create_worker_run_for_token,
)
from backend.api.schemas.worker import WorkerRunCreateRequest, WorkerTokenCreateRequest
from backend.strategies import service as strategy_service
from backend.strategies.attribution import RunBindingInput
from backend.strategies.models import StrategyJob
from backend.strategies.redaction import redact_text
from backend.strategies.repository import SqlAlchemyStrategyRepository

logger = logging.getLogger(__name__)

#: Bounded log shipping contract (supervisor -> API). Per chunk and per attempt.
LOG_CHUNK_MAX_BYTES = 16 * 1024
LOG_TOTAL_MAX_BYTES = 256 * 1024

__all__ = [
    "HostedLifecycleError",
    "HostedLifecycleHooks",
    "expire",
    "heartbeat",
    "job_source",
    "job_state",
    "list_jobs",
    "prepare_launch",
    "release",
    "fence",
    "report_process_cleanup",
    "require_job_authority",
]

_LIVE_STATUSES = ("starting", "running")


class HostedLifecycleError(Exception):
    """A lifecycle request is refused. ``reason`` is a stable machine code."""

    def __init__(self, status_code: int, reason: str, **extra: Any) -> None:
        detail: Dict[str, Any] = {"rejection_reason": reason}
        detail.update(extra)
        super().__init__(reason)
        self.status_code = status_code
        self.detail = detail


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def require_job_authority(
    job: Optional[StrategyJob],
    *,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    allow_statuses=_LIVE_STATUSES,
    require_live_lease: bool = True,
    require_started: bool = True,
) -> StrategyJob:
    """Authorize a lifecycle request against the persisted job authority.

    This is the only way a lifecycle request touches a job. It never accepts a
    bare run id: identity and configuration are derived from the job record, and
    the lease owner/epoch/attempt must all match.

    ``require_started=False`` is for **terminal reads only**: after a
    released-unlaunched attempt, ``desired_state`` is ``stopped`` and a state
    read must still succeed. It never relaxes the live-status/lease gates that
    gate mutation, so reads cannot regain mutation authority.
    """
    if job is None:
        raise HostedLifecycleError(404, "HOSTED_JOB_NOT_FOUND")
    if (
        str(job.lease_owner or "") != lease_owner
        or int(job.lease_epoch) != int(lease_epoch)
        or int(job.attempt) != int(attempt)
    ):
        raise HostedLifecycleError(
            403, "HOSTED_LEASE_AUTHORITY_MISMATCH", job_status=str(job.status or "")
        )
    if require_started and str(job.desired_state or "") != "started":
        raise HostedLifecycleError(409, "HOSTED_ATTEMPT_STOPPED")
    if str(job.status or "") not in allow_statuses:
        raise HostedLifecycleError(
            409, "HOSTED_ATTEMPT_FENCED", job_status=str(job.status or "")
        )
    if require_live_lease:
        if job.lease_until is None or _as_utc(job.lease_until) <= _utcnow():
            raise HostedLifecycleError(409, "HOSTED_LEASE_EXPIRED")
    return job


class HostedLifecycleHooks:
    """Injectable seams for tests (no behaviour change in production)."""

    def __init__(
        self,
        *,
        run_id_factory=None,
        token_factory=None,
    ) -> None:
        self.run_id_factory = run_id_factory or (lambda: f"run_{uuid.uuid4().hex}")
        self.token_factory = token_factory or (
            lambda: (f"worker_{uuid.uuid4().hex[:16]}", f"kwa_{secrets.token_urlsafe(32)}")
        )


async def _fail_closed(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    token_id: Optional[str],
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str,
) -> Dict[str, Any]:
    """Best-effort cleanup after a failed preparation step.

    Revokes any child authority and fences the attempt to ``recovery_required``.
    It is **best-effort and retryable**: it returns whether the durable fence was
    actually written, and callers must report that honestly rather than claiming
    that a failed write guarantees fencing. ``token_id`` is passed explicitly
    (the reserved identity), because the job object loaded at the start of the
    request does not yet contain it.
    """
    try:
        await asyncio.to_thread(
            strategy_repo.record_failure,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            reason=reason,
        )
    except Exception:  # pragma: no cover - diagnostic only
        logger.warning("hosted_lifecycle_record_failure_failed", extra={"job_id": job_id})

    revoked = False
    if token_id:
        try:
            revoked = (await worker_repo.revoke_token(token_id)) is not None
        except Exception:  # pragma: no cover - best effort
            logger.warning("hosted_lifecycle_revoke_token_failed", extra={"job_id": job_id})

    fenced = False
    try:
        fenced = bool(
            await asyncio.to_thread(
                strategy_repo.mark_recovery_required,
                job_id,
                lease_owner=lease_owner,
                expected_lease_epoch=lease_epoch,
                expected_attempt=attempt,
            )
        )
    except Exception:  # pragma: no cover - fencing is idempotent/retryable
        logger.warning("hosted_lifecycle_mark_recovery_failed", extra={"job_id": job_id})
    return {"fenced": fenced, "revoked": revoked}


def _capability_actions(capabilities: Dict[str, bool]) -> list:
    # Actions are derived from the version/job capability snapshot: trade grants
    # paper order actions, notify grants notifications:publish, data grants the
    # baseline read/log. ``heartbeat`` is never granted (lifecycle is
    # supervisor-owned).
    return strategy_service.capability_actions(capabilities)


def _protection_runtime_state(
    stale_exit_policy: str, progress_deadline_s: int
) -> Optional[Dict[str, Any]]:
    """Map the pinned stale-exit policy through the validated protection model.

    Only ``exit_on_worker_stale`` is a supported hosted policy; it installs the
    existing backend-protection runtime config so run creation validates it
    through the same path as an external worker. The stale threshold is the
    job's **pinned** progress deadline (clamped to the model's accepted range),
    so the policy cannot drift with later config. ``none`` installs nothing.
    """
    if stale_exit_policy == "exit_on_worker_stale":
        stale_sec = min(max(int(progress_deadline_s), 30), 86400)
        return {
            "enabled": True,
            "operations": {"exit_on_worker_stale": True, "worker_stale_sec": stale_sec},
        }
    return None


#: Worker-run statuses that still OWN their protection. An owner row whose run is
#: in one of these is never taken over by a new attempt: the live run stays
#: authoritative, and the successor must not claim protection it does not own.
_LIVE_RUN_STATUSES = ("open", "exiting")


def _protection_owner_store(strategy_repo: Any) -> Any:
    """The owner-row store on the SAME database as the job and run records."""

    from backend.options.protection.ownership import OptionProtectionOwnerStore

    session_factory = getattr(strategy_repo, "session_factory", None)
    if session_factory is None:
        return None
    return OptionProtectionOwnerStore(session_factory=session_factory)


def _protection_structure_from_policy(policy: Any) -> Optional[Dict[str, Any]]:
    """The run-level structure identity a frozen option policy names.

    The owner row's ``policy`` is the frozen snapshot the option evaluator reads.
    The successor's ``backend_protection.structure`` is seeded from the SAME
    digest, so the generic loop and the option evaluator can never disagree about
    which structure they are protecting (design section 2 step 1).
    """

    digest = str((policy or {}).get("structure_digest") or "").strip()
    if not digest:
        return None
    return {"structure_digest": digest}


async def _handover_protection_rows(
    *,
    strategy_repo: Any,
    worker_repo: Any,
    job: Any,
) -> List[Dict[str, Any]]:
    """ACTIVE owner rows a successor INHERITS from this strategy's previous run.

    A successor continues the strategy's durable book, so the structures that the
    predecessor's owner rows still carry move to it. The predecessor is the run
    being continued: its owner run has ENDED (the continuation closed it). A row
    whose owner run is still live is NEVER taken over - the successor must not
    claim protection it does not own, and the live owner stays authoritative.

    An unreadable read returns no rows rather than refusing the launch: the owner
    row is what protects the structure, and a launch must not become impossible
    because an optional bookkeeping read failed.
    """

    try:
        store = _protection_owner_store(strategy_repo)
        if store is None:
            return []
        rows = await asyncio.to_thread(
            store.list_protection_owners,
            None,
            strategy_id=str(job.strategy_id),
            account_id=str(job.account_scope),
            execution_environment=str(job.execution_mode),
        )
    except Exception:  # noqa: BLE001 - an optional read never blocks a launch
        logger.exception(
            "hosted_protection_owner_read_failed",
            extra={"job_id": str(getattr(job, "id", ""))},
        )
        return []

    inherited: List[Dict[str, Any]] = []
    for row in rows or []:
        owner_run_id = str(row.get("owner_run_id") or "")
        if not owner_run_id:
            continue
        try:
            run = await worker_repo.get_run(owner_run_id)
        except Exception:  # noqa: BLE001 - an unreadable owner run is not taken over
            run = None
        if run is None:
            continue
        if str(run.get("status") or "") in _LIVE_RUN_STATUSES:
            continue
        inherited.append(dict(row))
    return inherited


async def _transfer_inherited_structures(
    *,
    owner_store: Any,
    rows: List[Dict[str, Any]],
    successor_run_id: str,
) -> Optional[Dict[str, Any]]:
    """Move every inherited structure to the successor in ONE CAS each.

    Returns ``None`` when every row moved, or the refused row's named detail
    (``{"reason_code", "option_run_id", "detail"}``) when a compare-and-swap was
    LOST. A lost CAS means the structure moved under this successor: the row is
    left exactly as it was - the winner (normally the predecessor) stays
    authoritative - and the caller refuses the launch by name rather than
    pretending the successor owns protection it does not.
    """

    for row in rows:
        option_run_id = str(row.get("option_run_id") or "")
        if not option_run_id:
            continue
        if str(row.get("owner_run_id") or "") == str(successor_run_id):
            # A repeat preparation for this same successor: already transferred.
            continue
        try:
            await asyncio.to_thread(
                owner_store.transfer,
                option_run_id,
                successor_run_id,
                int(row.get("owner_epoch") or 0),
                dict(row.get("policy") or {}),
                row.get("policy_version"),
            )
        except Exception as exc:  # noqa: BLE001 - reported as its own named refusal
            return {
                "reason_code": str(
                    getattr(exc, "reason_code", type(exc).__name__)
                ),
                "option_run_id": option_run_id,
                "detail": dict(getattr(exc, "detail", {}) or {}),
            }
    return None


async def prepare_launch(
    request: Request,
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    hooks: Optional[HostedLifecycleHooks] = None,
) -> Dict[str, Any]:
    """Prepare a launch and return the child configuration (one-time handoff).

    The returned mapping contains the **plaintext child token**. It must be
    delivered to the supervisor over the authenticated channel and never logged.
    """
    hooks = hooks or HostedLifecycleHooks()

    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )

    # LAUNCH gate: ``HOSTED_LIVE_ENABLED`` (default false) gates minting a child
    # credential for a LIVE attempt, not just the submission that would use it.
    # Refusing here means a deployment with the setting off can never hand a
    # child the authority to trade live, even if the job row predates the flip.
    from backend.strategies.live_settings import (
        hosted_live_disabled_detail,
        hosted_live_enabled,
    )

    if str(job.execution_mode or "").lower() == "live" and not hosted_live_enabled():
        raise HostedLifecycleError(
            409,
            "LIVE_DISABLED",
            detail=hosted_live_disabled_detail(surface="hosted_prepare"),
        )

    # A completed handoff or an in-flight/reserved preparation is terminal for
    # this attempt: it must never mint a second credential and never replay the
    # launch. We do NOT fence here: a concurrent duplicate (or a supervisor that
    # lost the response) must not destroy a healthy attempt that another caller
    # may still own. Recovery is explicit — the caller uses ``fence``/``release``
    # (or a prior failed preparation already fenced the job).
    if job.handoff_at is not None:
        raise HostedLifecycleError(409, "HOSTED_HANDOFF_ALREADY_COMPLETED")
    if job.token_id is not None:
        raise HostedLifecycleError(409, "HOSTED_PREPARE_INCOMPLETE")

    # Capabilities are parsed from the pinned snapshot BEFORE anything is
    # minted or reserved. An ambiguous/marker-only legacy snapshot fails closed
    # (no trading rights are inferred).
    try:
        capabilities = strategy_service.parse_capability_snapshot(job.capabilities_snapshot)
    except strategy_service.StrategyValidationError as exc:
        raise HostedLifecycleError(
            409, "HOSTED_CAPABILITIES_AMBIGUOUS", detail=str(exc)
        ) from exc

    strategy = await asyncio.to_thread(
        strategy_repo.get_strategy, job.owner_id, job.strategy_id
    )
    if strategy is None:
        raise HostedLifecycleError(409, "HOSTED_STRATEGY_MISSING")
    version = await asyncio.to_thread(
        strategy_repo.get_version_by_id, job.strategy_id, job.version_id
    )
    if version is None:
        raise HostedLifecycleError(409, "HOSTED_VERSION_MISSING")
    template_id = strategy_service.template_id_for(job.strategy_id)

    async def _abort(reason: str, *, token: Optional[str], status_code: int, code: str):
        cleanup = await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job_id=job_id,
            token_id=token,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason=reason,
        )
        raise HostedLifecycleError(
            status_code,
            code,
            fencing="confirmed" if cleanup["fenced"] else "unconfirmed",
            token_revoked=cleanup["revoked"],
        )

    token_id, raw_token = hooks.token_factory()
    reserved = await asyncio.to_thread(
        strategy_repo.reserve_child_token,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
        token_id=token_id,
    )
    if not reserved:
        # Another supervisor won the preparation race (or state moved on). Do not
        # fence: the winner owns this attempt. Exactly one caller proceeds.
        raise HostedLifecycleError(409, "HOSTED_PREPARE_INCOMPLETE")

    expires_at = _utcnow() + timedelta(seconds=int(job.max_duration_s) + 300)
    token_payload = WorkerTokenCreateRequest(
        name=f"{template_id}:attempt-{attempt}",
        account_scope=job.account_scope,
        allowed_modes=[job.execution_mode],
        allowed_actions=_capability_actions(capabilities),
        allowed_templates=[template_id],
        expires_at=expires_at,
        metadata={
            "source": "hosted_supervisor",
            "hosted_job_id": job_id,
            "hosted_strategy_id": job.strategy_id,
            "hosted_attempt": int(attempt),
            "hosted_capabilities": dict(capabilities),
        },
    )

    try:
        await worker_repo.create_token(token_payload, raw_token=raw_token, token_id=token_id)
    except Exception as exc:
        await _abort(
            f"token_mint_failed: {type(exc).__name__}",
            token=token_id,
            status_code=503,
            code="HOSTED_TOKEN_MINT_FAILED",
        )

    child_token = WorkerToken(
        token_id=token_id,
        name=token_payload.name,
        account_scope=job.account_scope,
        allowed_modes=[job.execution_mode],
        allowed_actions=list(token_payload.allowed_actions),
        allowed_templates=[template_id],
        status="active",
        expires_at=expires_at,
    )

    stale_exit_policy = str((job.policy_snapshot or {}).get("stale_exit_policy") or "none")
    # A job created by a schedule occurrence carries its bound evaluation
    # identity. It is *config the child reads*, not authority the child invents:
    # the proposal route still derives run/job/strategy/account from the
    # persisted records and refuses an evaluation id that does not match this
    # binding.
    bound = dict(job.identity_json or {})
    occurrence_key = job.occurrence_key
    bound_evaluation_id = str(bound.get("evaluation_id") or "").strip() or None
    runtime_state: Dict[str, Any] = {
        "hosted": {
            "job_id": job_id,
            "strategy_id": job.strategy_id,
            "attempt": int(attempt),
            "version_id": job.version_id,
            "capabilities": dict(capabilities),
            "occurrence_key": occurrence_key,
            "evaluation_id": bound_evaluation_id,
            "evaluation_kind": bound.get("evaluation_kind"),
            "due_at": bound.get("due_at"),
        }
    }
    protection = _protection_runtime_state(stale_exit_policy, job.progress_deadline_s)
    if protection is not None:
        # Installed through the same validated run-creation path as an external
        # worker: run creation normalizes it and seeds the protection state.
        runtime_state["backend_protection"] = protection

    # A successor CONTINUES the strategy's durable book: every owner row the
    # predecessor's ended run still holds is read HERE, so the successor's run
    # config is seeded from the SAME frozen policy the owner row carries - and so
    # the transfer below can CAS on the epoch observed before the run existed.
    handover_rows = await _handover_protection_rows(
        strategy_repo=strategy_repo,
        worker_repo=worker_repo,
        job=job,
    )
    # Only a DECLARED run-level policy is seeded with the structure identity: a
    # strategy that declares none keeps exactly the run config it had, and the
    # owner row alone carries its structure.
    if protection is not None and len(handover_rows) == 1:
        structure = _protection_structure_from_policy(handover_rows[0].get("policy"))
        if structure is not None:
            runtime_state["backend_protection"] = {**protection, "structure": structure}
            # The epoch this run is ABOUT to be handed: the transfer below CASes
            # on the observed epoch, so the successor's own record and the owner
            # row agree, and a later handover can detect a superseded owner.
            runtime_state["protection_owner"] = {
                "option_run_id": str(handover_rows[0].get("option_run_id") or ""),
                "owner_epoch": int(handover_rows[0].get("owner_epoch") or 0) + 1,
                "policy_version": handover_rows[0].get("policy_version"),
            }

    run_id = hooks.run_id_factory()
    run_payload = WorkerRunCreateRequest(
        strategy_run_id=run_id,
        template_id=template_id,
        account_scope=job.account_scope,
        execution_mode=job.execution_mode,
        metadata={
            "strategy_name": strategy.name,
            "strategy_family": "indicator_strategy",
            "entry_surface": "hosted_supervisor",
            "hosted_job_id": job_id,
            "hosted_strategy_id": job.strategy_id,
            "hosted_attempt": int(attempt),
            "hosted_params": dict(job.params_snapshot or {}),
            "hosted_capabilities": dict(capabilities),
            "hosted_stale_exit_policy": stale_exit_policy,
            "hosted_occurrence_key": occurrence_key,
            "hosted_evaluation_id": bound_evaluation_id,
        },
        runtime_state=runtime_state,
    )

    try:
        await create_worker_run_for_token(
            request,
            child_token,
            run_payload,
            strategy_run_id=run_id,
            # Identity comes from the PERSISTED JOB, never from run metadata
            # (`metadata.hosted_strategy_id` is informational only and is not
            # proof of anything).
            binding=RunBindingInput(
                strategy_id=job.strategy_id,
                owner_id=job.owner_id,
                account_id=job.account_scope,
                execution_environment=job.execution_mode,
                bound_by="supervisor",
                binding_source="hosted_job",
            ),
        )
    except Exception as exc:
        await _abort(
            "run_create_failed",
            token=token_id,
            status_code=503,
            code="HOSTED_RUN_CREATE_FAILED",
        )

    # Transfer the continued structures to the successor. It runs RIGHT AFTER the
    # run exists (never before: an owner row must not name a run that was never
    # created) and in ONE compare-and-swap per structure under the option run's
    # advisory lock. Until that CAS commits the predecessor stays authoritative,
    # so there is no interval in which the structure is ownerless.
    owner_store = _protection_owner_store(strategy_repo) if handover_rows else None
    refused = (
        await _transfer_inherited_structures(
            owner_store=owner_store,
            rows=handover_rows,
            successor_run_id=run_id,
        )
        if owner_store is not None
        else None
    )
    if refused is not None:
        # A LOST CAS means the structure moved under this successor: it must NOT
        # claim protection it does not own. The owner row is untouched (the
        # predecessor, or whoever won, stays authoritative), the child authority
        # is withdrawn, and the launch is refused BY NAME.
        logger.warning(
            "hosted_protection_owner_transfer_refused",
            extra={
                "job_id": job_id,
                "run_id": run_id,
                "option_run_id": str(refused.get("option_run_id") or ""),
                "reason": str(refused.get("reason_code") or ""),
            },
        )
        try:
            # Stop the successor's own run from being evaluated as a generic
            # protection owner now that it owns nothing.
            await worker_repo.update_run_status(run_id, "closed")
        except Exception:  # noqa: BLE001 - best effort on the abort path
            logger.exception(
                "hosted_protection_owner_transfer_run_close_failed",
                extra={"run_id": run_id},
            )
        await _abort(
            "protection_owner_conflict",
            token=token_id,
            status_code=409,
            code="OPTION_PROTECTION_OWNER_CONFLICT",
        )

    try:
        recorded = await asyncio.to_thread(
            strategy_repo.record_child_run,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            token_id=token_id,
            run_id=run_id,
        )
    except Exception:
        recorded = False
    if not recorded:
        await _abort(
            "run_record_failed",
            token=token_id,
            status_code=409,
            code="HOSTED_PREPARE_INCOMPLETE",
        )

    try:
        claimed = await worker_repo.claim_run_session(
            run_id,
            freshness_seconds=WORKER_SESSION_FRESHNESS_SECONDS,
            claimed_without_heartbeat_seconds=WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
        )
    except Exception:
        claimed = None
    session_nonce = str((claimed or {}).get("worker_session_nonce") or "")
    if not session_nonce:
        await _abort(
            "session_claim_failed",
            token=token_id,
            status_code=503,
            code="HOSTED_SESSION_CLAIM_FAILED",
        )

    try:
        handed_off = await asyncio.to_thread(
            strategy_repo.mark_running_and_handoff,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            run_id=run_id,
        )
    except Exception:
        handed_off = False
    if not handed_off:
        await _abort(
            "handoff_mark_failed",
            token=token_id,
            status_code=409,
            code="HOSTED_PREPARE_INCOMPLETE",
        )

    return {
        "job_id": job_id,
        "strategy_id": job.strategy_id,
        "attempt": int(attempt),
        "lease_epoch": int(lease_epoch),
        "run_id": run_id,
        "worker_token": raw_token,  # one-time secret: never log this
        "session_nonce": session_nonce,
        "template_id": template_id,
        "execution_mode": job.execution_mode,
        "account_scope": job.account_scope,
        "params": dict(job.params_snapshot or {}),
        "max_duration_s": int(job.max_duration_s),
        "progress_deadline_s": int(job.progress_deadline_s),
        "stale_exit_policy": stale_exit_policy,
        "version_id": version.id,
        "source_sha256": version.source_sha256,
    }


async def heartbeat(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    lease_until: datetime,
) -> Dict[str, Any]:
    """Runner-owned heartbeat: renew the job lease and the worker session.

    This deliberately does **not** write ``last_progress_at``. Runner liveness is
    not strategy progress; a parent heartbeat cannot assert that the child is
    healthy.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    # A stop request (desired_state=stopped) must not revoke the supervisor's
    # authority to heartbeat/observe; require_started=False keeps these usable.
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_started=False,
    )
    renewed = await asyncio.to_thread(
        strategy_repo.renew_lease,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
        lease_until=lease_until,
    )
    if not renewed:
        raise HostedLifecycleError(409, "HOSTED_LEASE_RENEW_REFUSED")

    session_heartbeat_at = None
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        nonce = str((run or {}).get("worker_session_nonce") or "")
        if nonce:
            updated = await worker_repo.record_run_heartbeat(job.run_id, expected_nonce=nonce)
            if updated is None:
                raise HostedLifecycleError(409, "HOSTED_SESSION_CONFLICT")
            # The worker repository returns a datetime; the response contract is a
            # string. Leaking the raw value fails response validation (HTTP 500 on
            # every heartbeat after a session exists).
            raw_heartbeat = updated.get("last_heartbeat_at")
            if hasattr(raw_heartbeat, "isoformat"):
                session_heartbeat_at = raw_heartbeat.isoformat()
            elif raw_heartbeat is not None:
                session_heartbeat_at = str(raw_heartbeat)
    return {
        "status": "ok",
        "job_id": job_id,
        "lease_until": lease_until.isoformat(),
        "last_heartbeat_at": session_heartbeat_at,
    }


def _attempt_continuation(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    session_factory: Any,
    job: Any,
    completion: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Try the automatic evaluation-continuation for a freshly fenced attempt.

    Returns ``None`` when the continuation could not even be attempted (no
    session factory, an unexpected failure) so the caller falls back to the
    manual reconciliation block. A returned dict always carries ``continued``.
    """
    factory = session_factory or getattr(strategy_repo, "session_factory", None)
    if factory is None:
        return None
    try:
        from backend.strategies.continuation import COMPLETION_UNKNOWN, ContinuationService

        service = ContinuationService(session_factory=factory, repository=strategy_repo)
        return service.attempt(
            owner_id=str(job.owner_id),
            strategy_id=str(job.strategy_id),
            completion_state=str(completion or COMPLETION_UNKNOWN),
            actor_id="host:supervisor_release",
        )
    except Exception:  # noqa: BLE001 - never let continuation break the release path
        logger.exception(
            "hosted_release_continuation_attempt_failed", extra={"job_id": str(getattr(job, "id", ""))}
        )
        return None


async def release(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    completion: Optional[str] = None,
    exit_code: Optional[int] = None,
    session_factory: Any = None,
) -> Dict[str, Any]:
    """Runner-owned stop: withdraw authority, then decide replacement safety.

    Stopping code/session authority is **not** exposure reconciliation. The
    server decides the terminal state from persisted evidence, never from a
    caller-supplied "flat" assertion:

    - an **unlaunched** attempt (no credential was ever handed off) could not
      have accepted work, so it is safely marked ``stopped`` and replacement is
      allowed;
    - a **launched** attempt may have accepted work, so it is fenced to
      ``recovery_required``. When the runner reports a **clean exit** of a finite
      evaluation, the host then attempts the automatic evaluation-continuation
      proof: an eligible attempt clears its own block (recording a distinct
      continuation audit) so the next evaluation reads the same durable book
      without an operator clicking "reconcile". A crash, operator stop, timeout
      or any missing evidence leaves the block in place for explicit
      reconciliation.

    Either way the session is released and the child token revoked. Open exposure
    is never asserted to be flat.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_started=False,
    )
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        nonce = str((run or {}).get("worker_session_nonce") or "")
        if nonce:
            await worker_repo.release_run_session(job.run_id, expected_nonce=nonce)
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)

    launched = job.handoff_at is not None
    if launched:
        # Persist the runner's end-of-child report BEFORE the fence, so the
        # durable marker survives a host crash between the fence and the
        # continuation attempt. It is a report, never a safety assertion: the
        # continuation decision still derives from persisted evidence.
        if completion in ("exited", "stop_requested", "timeout"):
            try:
                await asyncio.to_thread(
                    strategy_repo.report_completion,
                    job_id,
                    completion_state=str(completion),
                    exit_code=exit_code,
                    lease_owner=lease_owner,
                    expected_lease_epoch=lease_epoch,
                    expected_attempt=attempt,
                )
            except Exception:  # noqa: BLE001 - a report failure must not break release
                logger.exception(
                    "hosted_release_completion_report_failed", extra={"job_id": job_id}
                )
        fenced = await asyncio.to_thread(
            strategy_repo.mark_recovery_required,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
        )
        if not fenced:
            raise HostedLifecycleError(409, "HOSTED_RELEASE_REFUSED")
        # The automatic continuation runs AFTER the fence: the child's authority
        # is already revoked above, and the fence is what makes the job's status
        # (``recovery_required``) match the CAS the unblock requires.
        continuation = await asyncio.to_thread(
            _attempt_continuation,
            strategy_repo=strategy_repo,
            session_factory=session_factory,
            job=job,
            completion=completion,
        )
        if continuation is not None and continuation.get("continued"):
            return {
                "status": "recovery_required",
                "job_id": job_id,
                "replacement_blocked": False,
                "reason": "evaluation_continuation_cleared",
                "continuation": continuation,
            }
        response = {
            "status": "recovery_required",
            "job_id": job_id,
            "replacement_blocked": True,
            "reason": "launched_attempt_requires_reconciliation",
        }
        if continuation is not None:
            response["continuation"] = continuation
        return response

    stopped = await asyncio.to_thread(
        strategy_repo.mark_stopped,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not stopped:
        raise HostedLifecycleError(409, "HOSTED_RELEASE_REFUSED")
    return {
        "status": "stopped",
        "job_id": job_id,
        "replacement_blocked": False,
        "reason": "unlaunched_attempt",
    }


async def expire(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str = "lease_expired",
) -> Dict[str, Any]:
    """Authenticated lease-loss recovery for an EXPIRED attempt.

    The ordinary ``fence`` requires a live lease, so an attempt whose lease has
    already lapsed cannot be fenced through it. This path authorizes by full
    identity (id + ``lease_owner`` + epoch + attempt) but **only** accepts an
    expired lease for a live attempt; it never renews a lease and never restores
    execution authority — it durably fences the attempt to ``recovery_required``
    and revokes the child token. A still-live lease is refused (use ``fence``).
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_live_lease=False,
        require_started=False,
    )
    if job.lease_until is not None and _as_utc(job.lease_until) > _utcnow():
        raise HostedLifecycleError(409, "HOSTED_LEASE_STILL_LIVE")
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)
    fenced = await asyncio.to_thread(
        strategy_repo.expire_to_recovery_authorized,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not fenced:
        raise HostedLifecycleError(409, "HOSTED_FENCE_REFUSED")
    return {
        "status": "recovery_required",
        "job_id": job_id,
        "replacement_blocked": True,
        "reason": reason,
    }


async def fence(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str = "fenced_by_supervisor",
) -> Dict[str, Any]:
    """Runner-owned fence to ``recovery_required``; revokes child authority.

    No automatic replay: a new attempt requires a new run and token and cannot
    start until the job is reconciled.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_started=False,
    )
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)
    fenced = await asyncio.to_thread(
        strategy_repo.mark_recovery_required,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not fenced:
        raise HostedLifecycleError(409, "HOSTED_FENCE_REFUSED")
    return {"status": "recovery_required", "job_id": job_id, "reason": reason}


async def job_state(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
) -> Dict[str, Any]:
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        allow_statuses=("starting", "running", "recovery_required", "stopped", "failed", "hung", "fencing"),
        require_live_lease=False,
        require_started=False,
    )
    run_status = None
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        if run is not None:
            run_status = run.get("status")
    return {
        "job_id": job.id,
        "strategy_id": job.strategy_id,
        "attempt": int(job.attempt),
        "status": job.status,
        "desired_state": job.desired_state,
        "lease_owner": job.lease_owner,
        "lease_epoch": int(job.lease_epoch),
        "lease_until": _as_utc(job.lease_until).isoformat() if job.lease_until else None,
        "run_id": job.run_id,
        "token_id": job.token_id,
        "handoff_at": _as_utc(job.handoff_at).isoformat() if job.handoff_at else None,
        "last_progress_at": _as_utc(job.last_progress_at).isoformat() if job.last_progress_at else None,
        "progress_deadline_s": int(job.progress_deadline_s),
        "process_cleanup_state": job.process_cleanup_state,
        "process_cleanup_at": _as_utc(job.process_cleanup_at).isoformat() if job.process_cleanup_at else None,
        "run_status": run_status,
    }


def list_jobs(*, strategy_repo: SqlAlchemyStrategyRepository, statuses=("queued",), limit: int = 50):
    """Narrow discovery: the oldest jobs awaiting a supervisor.

    Read-only and bounded. Only jobs whose ``desired_state`` is ``started`` are
    returned, so a stopped/paused job is never re-offered. This deliberately does
    not implement scheduling — it merely lets a supervisor find work.
    """
    rows = strategy_repo.list_jobs_by_status(tuple(statuses), limit=limit)
    return [
        {
            "job_id": row.id,
            "strategy_id": row.strategy_id,
            "attempt": int(row.attempt),
            "status": row.status,
            "execution_mode": row.execution_mode,
            "lease_epoch": int(row.lease_epoch),
            "created_at": _as_utc(row.created_at).isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def job_source(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
) -> Dict[str, Any]:
    """Deliver the exact pinned source/version/hash to an *authorized* supervisor.

    Requires a **live** lease on a live attempt, so source can only be fetched
    while the supervisor legitimately owns the launch. The source is returned as
    text for the supervisor to persist and hash-verify; it is never imported by
    the API.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )
    version = await asyncio.to_thread(
        strategy_repo.get_version_by_id, job.strategy_id, job.version_id
    )
    if version is None:
        raise HostedLifecycleError(409, "HOSTED_VERSION_MISSING")
    return {
        "job_id": job.id,
        "strategy_id": job.strategy_id,
        "version_id": version.id,
        "version": int(version.version),
        "source": version.source,
        "source_sha256": version.source_sha256,
    }


async def report_process_cleanup(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    state: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Record supervisor-owned process-cleanup evidence, bound to the attempt.

    Only the authenticated supervisor reaches this (the child has no lifecycle
    credential), so it cannot forge cleanup evidence. Authority is the same
    lease/attempt identity, allowing a report after the attempt has been fenced
    (the lease owner/epoch/attempt are retained as attribution).
    """
    if state not in ("confirmed", "unresolved"):
        raise HostedLifecycleError(422, "HOSTED_PROCESS_CLEANUP_STATE_INVALID")
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        allow_statuses=(
            "starting",
            "running",
            "fencing",
            "recovery_required",
            "stopped",
            "failed",
            "hung",
        ),
        require_live_lease=False,
        require_started=False,
    )
    recorded = await asyncio.to_thread(
        strategy_repo.report_process_cleanup,
        job_id,
        state=state,
        actor=lease_owner,
        expected_attempt=int(attempt),
    )
    if not recorded:
        raise HostedLifecycleError(409, "HOSTED_PROCESS_CLEANUP_REFUSED")
    return {
        "job_id": job_id,
        "attempt": int(attempt),
        "process_cleanup_state": state,
        "note": (str(note)[:200] if note else None),
    }


def _split_by_bytes(text: str, max_bytes: int) -> List[str]:
    """Split ``text`` into pieces of at most ``max_bytes`` UTF-8 bytes.

    Cuts only at character boundaries so a multi-byte character is never split.
    """
    pieces: List[str] = []
    current: List[str] = []
    current_bytes = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if current and current_bytes + size > max_bytes:
            pieces.append("".join(current))
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += size
    if current:
        pieces.append("".join(current))
    return pieces


async def report_job_logs(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    chunks,
) -> Dict[str, Any]:
    """Accept bounded, redacted child-log chunks from the supervisor.

    Request chunks are joined, **redacted as one text** (so a credential split
    across transport chunk boundaries in the same request is still masked), then
    re-split on UTF-8 character boundaries. Byte accounting is exact; the cap is
    enforced by the store, which reports ``discarded`` when output was dropped.
    The API never reads the supervisor container's filesystem.

    Redaction limits: it masks token-shaped runs and known configured secrets; it
    cannot guarantee removal of an arbitrary secret, and a secret split across
    *separate* ingestion requests may not be masked. Log collection is
    independent of process-cleanup confirmation.
    """
    if not chunks:
        raise HostedLifecycleError(422, "HOSTED_LOGS_EMPTY")
    for chunk in chunks:
        if len(str(chunk).encode("utf-8")) > LOG_CHUNK_MAX_BYTES:
            raise HostedLifecycleError(413, "HOSTED_LOG_CHUNK_TOO_LARGE")
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        allow_statuses=(
            "starting",
            "running",
            "fencing",
            "recovery_required",
            "stopped",
            "failed",
            "hung",
        ),
        require_live_lease=False,
        require_started=False,
    )
    redacted = redact_text("".join(str(chunk) for chunk in chunks))
    pieces = _split_by_bytes(redacted, LOG_CHUNK_MAX_BYTES)
    result = await asyncio.to_thread(
        strategy_repo.append_job_log,
        job_id,
        attempt=int(attempt),
        chunks=pieces,
        max_total_bytes=LOG_TOTAL_MAX_BYTES,
    )
    return {
        "job_id": job.id,
        "attempt": int(attempt),
        "stored": int(result["stored"]),
        "truncated": bool(result["truncated"]),
        "discarded": bool(result.get("discarded", result["truncated"])),
        "next_seq": int(result["next_seq"]),
    }
