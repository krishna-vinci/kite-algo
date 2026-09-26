"""Durable, idempotent execution requests and their dispatch claim (Phase 2).

A proposal stays a proposal until execution is *requested*. This module owns that
request: one durable row per ``(owner, plan, idempotency key)``, its decision
evidence, the reservation/approval links the shared pipeline produces, and the
durable dispatch claim that lets a bounded background dispatcher act on it
without replaying an uncertain submission.

The rules that make it safe, stated once:

* **The request records a mode, the mode is not the request.** The authorization
  mode is read from the persisted hosted strategy, never from the caller, and an
  ``autonomous`` request queues only under a matching current grant.
* **Approval marks work ready.** An owner decision flips the row to ``queued`` in
  one transaction, so a dropped HTTP response cannot lose it. Admission,
  reservation, structural approval and execution all happen later, through the
  same shared pipeline the operator routes use.
* **Claim acquisition and grant revocation take the SAME lock** (the hosted
  strategy row). A revocation that wins denies the claim; a claim that already
  won may have reached the broker, and nothing here pretends otherwise.
* **A claim is not acceptance.** A dispatcher that dies mid-claim leaves a
  ``dispatching`` row. Recovery marks it ``executed`` only when the plan's own
  append-only trail proves the submission happened, and otherwise marks it
  ``dispatch_unresolved`` - never a blind replay.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import StrategyPlan, StrategyReservation
from backend.strategies.execution_authorization import (
    ExecutionAuthorizationService,
    sha256_json,
)
from backend.strategies.models import (
    HostedExecutionAudit,
    HostedExecutionRequest,
    HostedStrategy,
    StrategyJob,
)
from backend.strategies.plan_pipeline import PlanExecutionPipeline, PipelineRefusal

#: The attempt statuses that may spend authority (mirrors the hosted-attempt
#: guard in ``backend/api/services/hosted_attempt.py``).
ATTEMPT_AUTHORITY_STATUSES = ("starting", "running")

#: Request states from which the dispatcher may claim work.
DISPATCHABLE_STATUSES = ("queued",)

#: Request states that can have dispatched a plan's work. A dependent release is
#: bound to the request that started the plan, whatever the strategy's mode says
#: later.
GOVERNING_REQUEST_STATUSES = ("executed", "dispatching", "dispatch_unresolved")

#: Request states that are final.
TERMINAL_STATUSES = ("executed", "refused", "rejected", "dispatch_unresolved")

#: How long a claim may be in flight before recovery inspects it.
DEFAULT_CLAIM_TIMEOUT_SECONDS = 900

#: Plan-trail events that only a real submission attempt produces. ``submitted``
#: is written in the same transaction that commits the attempt to the runtime;
#: ``filled``/``partially_filled``/``failed`` follow a wire attempt. ``no_op``
#: (nothing was needed) and a bare ``rejected`` (usually a pre-send refusal) are
#: deliberately absent - they are not proof that anything reached the broker.
SUBMISSION_PROOF_EVENTS = ("submitted", "filled", "partially_filled", "failed")

#: Live claim states that mean the step has already left the platform for the
#: broker. ``withheld``/``releasing`` are excluded deliberately: the claim row is
#: materialized BEFORE the send, so neither proves a submission happened.
SUBMITTED_CLAIM_STATES = (
    "pending",
    "partial",
    "finalizing",
    "rejecting",
    "repair_required",
    "uncertain",
)

#: Live claim states that mean a release BEGAN but no outcome was recorded. The
#: send may or may not have reached the broker, so recovery must treat these as
#: genuinely unknown rather than as "nothing was sent".
RELEASE_ATTEMPTED_CLAIM_STATES = ("releasing", "partial", "finalizing", "rejecting")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _json_safe(value: Any) -> Any:
    """Coerce a detail payload into something a JSON column can store.

    Datetimes/dates become ISO strings, ``Decimal`` becomes ``float``, and
    mapping/list shapes are rebuilt recursively. Anything else is passed
    through untouched so a genuine mistake still surfaces.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _plan_summary(plan_kind: Any, logical_plan: Any, resolved_plan: Any) -> str:
    """A short, honest summary of a FROZEN plan for the approvals inbox.

    Built only from what the stored plan actually says - its kind and, when the
    frozen legs are present, how many of them there are. Nothing is re-derived
    from live market data or from the strategy's current configuration, so what
    the owner reads is what was frozen at request time.
    """
    kind = str(plan_kind or "").strip() or "plan"
    for candidate in (resolved_plan, logical_plan):
        legs = (candidate or {}).get("legs") if isinstance(candidate, Mapping) else None
        if isinstance(legs, list):
            return f"{kind} ({len(legs)} leg{'s' if len(legs) != 1 else ''})"
    return kind


class ExecutionRequestError(Exception):
    """A named refusal from the request service."""

    reason_code = "EXECUTION_REQUEST_ERROR"
    status_code = 409

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


class ExecutionRequestNotFound(ExecutionRequestError):
    reason_code = "EXECUTION_REQUEST_NOT_FOUND"
    status_code = 404


class ExecutionRequestStateError(ExecutionRequestError):
    reason_code = "EXECUTION_REQUEST_STATE_INVALID"
    status_code = 409


class ExecutionRequestConflict(ExecutionRequestError):
    reason_code = "EXECUTION_REQUEST_KEY_CONFLICT"
    status_code = 409


class ExecutionRequestInputError(ExecutionRequestError):
    reason_code = "EXECUTION_REQUEST_INPUT_INVALID"
    status_code = 422


class ExecutionRequestService:
    """Create, decide, claim and (via the pipeline) run execution requests."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        pipeline: Optional[PlanExecutionPipeline] = None,
        authorization: Optional[ExecutionAuthorizationService] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._pipeline = pipeline
        self._authorization = authorization
        self._clock = clock or _utcnow

    @property
    def pipeline(self) -> PlanExecutionPipeline:
        if self._pipeline is None:
            self._pipeline = PlanExecutionPipeline(self.session_factory)
        return self._pipeline

    @property
    def authorization(self) -> ExecutionAuthorizationService:
        if self._authorization is None:
            self._authorization = ExecutionAuthorizationService(self.session_factory)
        return self._authorization

    # -- reads --------------------------------------------------------------

    @staticmethod
    def _view(row: HostedExecutionRequest) -> Dict[str, Any]:
        return {
            "request_id": str(row.request_id),
            "owner_id": str(row.owner_id),
            "strategy_id": str(row.strategy_id),
            "canonical_strategy_id": str(row.canonical_strategy_id),
            "account_id": str(row.account_id),
            "execution_environment": str(row.execution_environment),
            "strategy_run_id": str(row.strategy_run_id),
            "job_id": row.job_id,
            "token_id": row.token_id,
            "attempt": row.attempt,
            "lease_epoch": row.lease_epoch,
            "version_id": str(row.version_id),
            "version_number": row.version_number,
            "source_sha256": str(row.source_sha256),
            "policy_hash": str(row.policy_hash),
            "evaluation_id": row.evaluation_id,
            "plan_id": str(row.plan_id),
            "plan_hash": str(row.plan_hash),
            "authorization_mode": str(row.authorization_mode),
            "grant_id": row.grant_id,
            "status": str(row.status),
            "refusal_code": row.refusal_code,
            "refusal_detail": dict(row.refusal_detail or {}),
            "decision_kind": row.decision_kind,
            "decision_actor": row.decision_actor,
            "decision_at": row.decision_at,
            "decision_evidence": dict(row.decision_evidence or {}),
            "approval_id": row.approval_id,
            "reservation_id": row.reservation_id,
            "execution_detail": dict(row.execution_detail or {}),
            "outcome_state": dict(row.execution_detail or {}).get("outcome_state"),
            "dispatch_claim_id": row.dispatch_claim_id,
            "dispatch_claimed_at": row.dispatch_claimed_at,
            "dispatch_started_at": row.dispatch_started_at,
            "dispatch_finished_at": row.dispatch_finished_at,
            "idempotency_key": str(row.idempotency_key),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "terminal": str(row.status) in TERMINAL_STATUSES,
            "executable": str(row.status) in DISPATCHABLE_STATUSES,
        }

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(HostedExecutionRequest).where(
                    HostedExecutionRequest.request_id == str(request_id)
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def get_for_owner(
        self, owner_id: str, strategy_id: str, request_id: str
    ) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(HostedExecutionRequest).where(
                    HostedExecutionRequest.request_id == str(request_id),
                    HostedExecutionRequest.owner_id == str(owner_id),
                    HostedExecutionRequest.strategy_id == str(strategy_id),
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def list_for_strategy(self, strategy_id: str, *, limit: int = 50) -> list:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(HostedExecutionRequest)
                    .where(HostedExecutionRequest.strategy_id == str(strategy_id))
                    .order_by(
                        HostedExecutionRequest.created_at.desc(),
                        HostedExecutionRequest.request_id,
                    )
                    .limit(int(limit))
                )
                .scalars()
                .all()
            )
            return [self._view(row) for row in rows]

    def list_for_run(self, strategy_run_id: str, *, limit: int = 50) -> list:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(HostedExecutionRequest)
                    .where(HostedExecutionRequest.strategy_run_id == str(strategy_run_id))
                    .order_by(
                        HostedExecutionRequest.created_at.desc(),
                        HostedExecutionRequest.request_id,
                    )
                    .limit(int(limit))
                )
                .scalars()
                .all()
            )
            return [self._view(row) for row in rows]

    def list_pending_for_owner(self, owner_id: str, *, limit: int = 100) -> list:
        """Every execution request awaiting THIS owner's decision, newest first.

        The approvals inbox is owner-scoped across strategies (an owner does not
        approve per strategy), so the filter is ``owner_id`` and the ONLY status
        is ``awaiting_approval``: a request already decided, refused, dispatching
        or executed is not pending and never appears here. Each row carries the
        strategy's name and a short frozen-plan summary so the owner can decide
        without a second round of reads, and the expiry of the reservation the
        request holds when it has one (``None`` until one exists - unknown is
        reported as unknown, never guessed).
        """
        owner = str(owner_id or "").strip()
        if not owner:
            return []
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    HostedExecutionRequest,
                    HostedStrategy.name,
                    StrategyPlan.plan_kind,
                    StrategyPlan.logical_plan,
                    StrategyPlan.resolved_plan,
                    StrategyReservation.valid_until,
                )
                .join(
                    HostedStrategy,
                    HostedStrategy.id == HostedExecutionRequest.strategy_id,
                )
                .join(
                    StrategyPlan, StrategyPlan.plan_id == HostedExecutionRequest.plan_id
                )
                .outerjoin(
                    StrategyReservation,
                    StrategyReservation.reservation_id
                    == HostedExecutionRequest.reservation_id,
                )
                .where(
                    HostedExecutionRequest.owner_id == owner,
                    HostedExecutionRequest.status == "awaiting_approval",
                )
                .order_by(
                    HostedExecutionRequest.created_at.desc(),
                    HostedExecutionRequest.request_id,
                )
                .limit(int(limit))
            ).all()
        return [
            {
                "strategy_id": str(row.strategy_id),
                "strategy_name": str(strategy_name or ""),
                "request_id": str(row.request_id),
                "plan_id": str(row.plan_id),
                "environment": str(row.execution_environment),
                "summary": _plan_summary(plan_kind, logical_plan, resolved_plan),
                "created_at": row.created_at,
                "expires_at": valid_until,
            }
            for row, strategy_name, plan_kind, logical_plan, resolved_plan, valid_until in rows
        ]

    # -- creation -----------------------------------------------------------

    def create_for_job(
        self,
        *,
        job: StrategyJob,
        plan_id: str,
        idempotency_key: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Create (or replay) the durable request for one frozen plan.

        Every identity except the idempotency key is derived from persisted
        records: the job supplies owner/strategy/account/version/attempt, the
        plan must agree with all of it, and the mode comes from the hosted
        strategy row. A manual request is recorded ``awaiting_approval``; an
        autonomous one is recorded ``queued`` only when a matching current grant
        says so, and ``refused`` (with the named reason) otherwise.
        """
        moment = now or self._clock()
        key = str(idempotency_key or "").strip()
        if not key:
            raise ExecutionRequestInputError({"message": "idempotency_key is required"})

        plan = self.pipeline.plan(plan_id)
        if plan is None:
            raise ExecutionRequestNotFound({"plan_id": str(plan_id)})
        strategy_id = str(job.strategy_id or "")
        account_id = str(job.account_scope or "")
        owner_id = str(job.owner_id or "")
        if str(plan.get("strategy_id") or "") != strategy_id:
            raise ExecutionRequestInputError(
                {
                    "rejection_reason": "PLAN_STRATEGY_MISMATCH",
                    "plan_id": str(plan_id),
                    "plan_strategy_id": str(plan.get("strategy_id") or ""),
                    "strategy_id": strategy_id,
                }
            )
        if str(plan.get("account_id") or "") != account_id:
            raise ExecutionRequestInputError(
                {
                    "rejection_reason": "PLAN_ACCOUNT_MISMATCH",
                    "plan_id": str(plan_id),
                    "plan_account_id": str(plan.get("account_id") or ""),
                    "account_id": account_id,
                }
            )

        binding = self.pipeline.binding(plan)
        environment = str(binding.get("execution_environment") or "")
        bound_run = str(binding.get("strategy_run_id") or "")
        if bound_run and bound_run != str(job.run_id or ""):
            raise ExecutionRequestInputError(
                {
                    "rejection_reason": "PLAN_RUN_MISMATCH",
                    "plan_id": str(plan_id),
                    "bound_run_id": bound_run,
                    "job_run_id": str(job.run_id or ""),
                }
            )

        with self.session_factory() as session:
            strategy = session.execute(
                select(HostedStrategy).where(HostedStrategy.id == strategy_id)
            ).scalar_one_or_none()
            if strategy is None:
                raise ExecutionRequestNotFound({"strategy_id": strategy_id})
            mode = str(strategy.authorization_mode or "approval_based")
            policy_snapshot = self.authorization.policy_snapshot(session, strategy)
            policy_hash = self.authorization.policy_hash_for(policy_snapshot)

        version_id = str(job.version_id or "")
        source_sha256 = self._version_source(job.strategy_id, version_id)
        request_hash = sha256_json(
            {
                "plan_id": str(plan["plan_id"]),
                "plan_hash": str(plan.get("plan_hash") or ""),
                "strategy_id": strategy_id,
                "account_id": account_id,
                "execution_environment": environment,
                "strategy_run_id": str(job.run_id or ""),
                "version_id": version_id,
                "source_sha256": source_sha256,
                "authorization_mode": mode,
            }
        )

        session = self.session_factory()
        try:
            existing = session.execute(
                select(HostedExecutionRequest).where(
                    HostedExecutionRequest.owner_id == owner_id,
                    HostedExecutionRequest.plan_id == str(plan["plan_id"]),
                    HostedExecutionRequest.idempotency_key == key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if str(existing.request_hash) != request_hash:
                    raise ExecutionRequestConflict(
                        {
                            "request_id": str(existing.request_id),
                            "idempotency_key": key,
                            "message": (
                                "this idempotency key was already used for a different "
                                "execution request"
                            ),
                        }
                    )
                view = self._view(existing)
                session.rollback()
                return {"idempotent": True, "request": view}

            request_id = str(uuid.uuid4())
            decision: Dict[str, Any] = {
                "status": "awaiting_approval",
                "grant_id": None,
                "decision_kind": None,
                "decision_actor": None,
                "decision_at": None,
                "decision_evidence": {},
                "refusal_code": None,
                "refusal_detail": {},
            }
            # An option plan this strategy's own durable work already blocks (an
            # ENTRY it owns, or an ADJUST whose basis or state has moved) is
            # refused HERE, by name: the owner must never be asked to approve a
            # plan the platform would refuse at execution anyway.
            structure_refusal = self._option_structure_refusal(
                plan=plan,
                strategy_id=strategy_id,
                account_id=account_id,
                execution_environment=environment,
                session=session,
            )
            if structure_refusal is not None:
                decision.update(
                    {
                        "status": "refused",
                        "refusal_code": structure_refusal["reason_code"],
                        "refusal_detail": {
                            "checked_at": moment.isoformat(),
                            "stage": "request",
                            **structure_refusal["detail"],
                        },
                    }
                )
            elif mode == "autonomous":
                evaluation = self.authorization.evaluate(
                    strategy_id=strategy_id,
                    mode=mode,
                    account_id=account_id,
                    environment=environment,
                    version_id=version_id,
                    source_sha256=source_sha256,
                    policy_hash=policy_hash,
                    now=moment,
                )
                if evaluation.get("authorized"):
                    grant = dict(evaluation.get("grant") or {})
                    decision.update(
                        {
                            "status": "queued",
                            "grant_id": str(grant.get("grant_id") or ""),
                            "decision_kind": "automatic",
                            "decision_actor": str(grant.get("issued_by") or ""),
                            "decision_at": moment,
                            "decision_evidence": {
                                "authorization_mode": "autonomous",
                                "grant_id": str(grant.get("grant_id") or ""),
                                "issued_by": str(grant.get("issued_by") or ""),
                                "version_id": version_id,
                                "source_sha256": source_sha256,
                                "account_id": account_id,
                                "execution_environment": environment,
                                "policy_hash": policy_hash,
                                "evaluated_at": moment.isoformat(),
                            },
                        }
                    )
                else:
                    decision.update(
                        {
                            "status": "refused",
                            "refusal_code": str(evaluation.get("refusal_code") or "GRANT_REQUIRED"),
                            "refusal_detail": {
                                "authorization_mode": mode,
                                "version_id": version_id,
                                "source_sha256": source_sha256,
                                "policy_hash": policy_hash,
                                "execution_environment": environment,
                            },
                        }
                    )

            row = HostedExecutionRequest(
                request_id=request_id,
                owner_id=owner_id,
                strategy_id=strategy_id,
                canonical_strategy_id=str(getattr(job, "strategy_id", "") or ""),
                account_id=account_id,
                execution_environment=environment,
                strategy_run_id=str(job.run_id or ""),
                job_id=str(job.id or ""),
                token_id=str(job.token_id or "") or None,
                attempt=int(job.attempt or 0),
                lease_epoch=int(job.lease_epoch or 0),
                version_id=version_id,
                version_number=self._version_number(strategy_id, version_id),
                source_sha256=source_sha256,
                policy_hash=policy_hash,
                evaluation_id=self._evaluation_id(job),
                plan_id=str(plan["plan_id"]),
                plan_hash=str(plan.get("plan_hash") or ""),
                authorization_mode=mode,
                grant_id=decision["grant_id"],
                status=decision["status"],
                refusal_code=decision["refusal_code"],
                refusal_detail=decision["refusal_detail"],
                decision_kind=decision["decision_kind"],
                decision_actor=decision["decision_actor"],
                decision_at=decision["decision_at"],
                decision_evidence=decision["decision_evidence"],
                idempotency_key=key,
                request_hash=request_hash,
                created_at=moment,
                updated_at=moment,
            )
            session.add(row)
            session.add(
                HostedExecutionAudit(
                    owner_id=owner_id,
                    strategy_id=strategy_id,
                    subject_kind="request",
                    subject_id=request_id,
                    event="requested",
                    actor_id=str(job.token_id or "worker"),
                    actor_kind="system",
                    detail={
                        "plan_id": str(plan["plan_id"]),
                        "plan_hash": str(plan.get("plan_hash") or ""),
                        "authorization_mode": mode,
                        "status": decision["status"],
                        "refusal_code": decision["refusal_code"],
                        "strategy_run_id": str(job.run_id or ""),
                        "attempt": int(job.attempt or 0),
                        "idempotency_key": key,
                    },
                    created_at=moment,
                )
            )
            session.commit()
            return {"idempotent": False, "request": self._view(row)}
        except ExecutionRequestError:
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            # Lost the race on UNIQUE (owner, plan, idempotency_key). The
            # contract is a REPLAY, not an error: re-read the winner and return
            # it when the content is identical, and only conflict when the same
            # key was reused for different content.
            existing = None
            with self.session_factory() as reader:
                existing = reader.execute(
                    select(HostedExecutionRequest).where(
                        HostedExecutionRequest.owner_id == owner_id,
                        HostedExecutionRequest.plan_id == str(plan["plan_id"]),
                        HostedExecutionRequest.idempotency_key == key,
                    )
                ).scalar_one_or_none()
                if existing is not None and str(existing.request_hash) != request_hash:
                    raise ExecutionRequestConflict(
                        {
                            "request_id": str(existing.request_id),
                            "idempotency_key": key,
                            "message": (
                                "this idempotency key was already used for a different "
                                "execution request"
                            ),
                        }
                    ) from exc
                view = self._view(existing) if existing is not None else None
            if view is not None:
                return {"idempotent": True, "request": view}
            raise ExecutionRequestConflict(
                {"idempotency_key": key, "message": "a concurrent request won this key"}
            ) from exc
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    # -- owner decisions ----------------------------------------------------

    def approve(
        self,
        request_id: str,
        *,
        owner_id: str,
        strategy_id: str,
        actor: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Owner authorises the exact requested plan; the row becomes dispatchable.

        Approving does NOT execute anything: it records the decision and marks the
        work ready under the same lock the dispatcher uses, so a dropped response
        cannot lose an approved action.
        """
        moment = now or self._clock()
        session = self.session_factory()
        try:
            row = self._lock_request(session, owner_id, strategy_id, request_id)
            if row is None:
                raise ExecutionRequestNotFound({"request_id": str(request_id)})
            if str(row.status) != "awaiting_approval":
                raise ExecutionRequestStateError(
                    {
                        "request_id": str(row.request_id),
                        "status": str(row.status),
                        "message": "only a request awaiting approval can be approved",
                    }
                )
            # The plan must still be the plan the request named, and the attempt
            # must still be authoritative. Reads only; no network call.
            plan = self.pipeline.plan(str(row.plan_id))
            if plan is None or str(plan.get("plan_hash") or "") != str(row.plan_hash):
                raise ExecutionRequestStateError(
                    {
                        "rejection_reason": "PLAN_HASH_MISMATCH",
                        "request_id": str(row.request_id),
                        "plan_id": str(row.plan_id),
                    }
                )
            # The state may have moved while the request waited: re-ask the
            # structural rule (the frozen phase selects it) before this becomes
            # dispatchable work.
            structure_refusal = self._option_structure_refusal(
                plan=plan,
                strategy_id=str(row.strategy_id),
                account_id=str(row.account_id),
                execution_environment=str(row.execution_environment),
                session=session,
            )
            if structure_refusal is not None:
                row.status = "refused"
                row.refusal_code = structure_refusal["reason_code"]
                row.refusal_detail = {
                    "checked_at": moment.isoformat(),
                    "stage": "approval",
                    **structure_refusal["detail"],
                }
                row.updated_at = moment
                session.add(self._audit(row, "refused", actor, "owner", moment))
                session.commit()
                return {"request": self._view(row), "approved": False}
            refusal = self._attempt_refusal(session, row, moment)
            if refusal is not None:
                row.status = "refused"
                row.refusal_code = refusal
                row.refusal_detail = {"checked_at": moment.isoformat(), "stage": "approval"}
                row.updated_at = moment
                session.add(self._audit(row, "refused", actor, "owner", moment))
                session.commit()
                return {"request": self._view(row), "approved": False}
            row.status = "queued"
            row.decision_kind = "manual"
            row.decision_actor = str(actor)
            row.decision_at = moment
            row.decision_evidence = {
                "authorization_mode": str(row.authorization_mode),
                "approved_by": str(actor),
                "plan_id": str(row.plan_id),
                "plan_hash": str(row.plan_hash),
                "version_id": str(row.version_id),
                "account_id": str(row.account_id),
                "execution_environment": str(row.execution_environment),
                "attempt": int(row.attempt or 0),
                "policy_hash": str(row.policy_hash),
            }
            row.updated_at = moment
            session.add(self._audit(row, "approved", actor, "owner", moment))
            session.commit()
            return {"request": self._view(row), "approved": True}
        except ExecutionRequestError:
            session.rollback()
            raise
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    def reject(
        self,
        request_id: str,
        *,
        owner_id: str,
        strategy_id: str,
        actor: str,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        moment = now or self._clock()
        session = self.session_factory()
        try:
            row = self._lock_request(session, owner_id, strategy_id, request_id)
            if row is None:
                raise ExecutionRequestNotFound({"request_id": str(request_id)})
            if str(row.status) != "awaiting_approval":
                raise ExecutionRequestStateError(
                    {
                        "request_id": str(row.request_id),
                        "status": str(row.status),
                        "message": "only a request awaiting approval can be rejected",
                    }
                )
            row.status = "rejected"
            row.refusal_code = "OWNER_REJECTED"
            row.refusal_detail = {"reason": reason}
            row.decision_kind = "manual"
            row.decision_actor = str(actor)
            row.decision_at = moment
            row.updated_at = moment
            session.add(self._audit(row, "rejected", actor, "owner", moment))
            session.commit()
            return {"request": self._view(row), "rejected": True}
        except ExecutionRequestError:
            session.rollback()
            raise
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    # -- dispatch -----------------------------------------------------------

    def claim_next(self, *, limit: int = 10, now: Optional[datetime] = None) -> list:
        """Claim up to ``limit`` queued requests, re-deriving authority first.

        The claim takes the hosted strategy row ``FOR UPDATE`` — the SAME lock a
        grant revocation takes — before it decides. Therefore a revocation that
        committed first denies the claim, and a claim that committed first has
        already been authorised: this method never claims it cancels a broker
        order.
        """
        moment = now or self._clock()
        claimed: list = []
        with self.session_factory() as session:
            candidates = (
                session.execute(
                    select(HostedExecutionRequest)
                    .where(HostedExecutionRequest.status.in_(DISPATCHABLE_STATUSES))
                    .order_by(
                        HostedExecutionRequest.created_at,
                        HostedExecutionRequest.request_id,
                    )
                    .limit(int(limit))
                )
                .scalars()
                .all()
            )
            for candidate in candidates:
                claimed.append(self._claim_one(session, candidate, moment))
            session.commit()
        return [row for row in claimed if row is not None]

    def _claim_one(
        self, session: Any, row: HostedExecutionRequest, moment: datetime
    ) -> Optional[Dict[str, Any]]:
        strategy_id = str(row.strategy_id)
        # The lock target. PostgreSQL serialises here; SQLite serialises the
        # whole transaction. Either way the decision below is not a race.
        strategy = self.authorization.lock_strategy(session, strategy_id)
        if strategy is None:
            self._refuse_claim(session, row, "STRATEGY_NOT_FOUND", {}, moment)
            return None
        # The row may have moved between the scan and the lock.
        session.refresh(row)
        if str(row.status) not in DISPATCHABLE_STATUSES:
            return None

        refusal = self._attempt_refusal(session, row, moment)
        if refusal is not None:
            self._refuse_claim(session, row, refusal, {}, moment)
            return None

        current_mode = str(strategy.authorization_mode or "approval_based")
        policy_snapshot = self.authorization.policy_snapshot(session, strategy)
        policy_hash = self.authorization.policy_hash_for(policy_snapshot)
        if policy_hash != str(row.policy_hash):
            code = (
                "GRANT_POLICY_CHANGED"
                if str(row.authorization_mode) == "autonomous"
                else "POLICY_CHANGED"
            )
            self._refuse_claim(
                session,
                row,
                code,
                {"policy_hash": str(row.policy_hash), "current_policy_hash": policy_hash},
                moment,
            )
            return None

        if str(row.authorization_mode) == "autonomous":
            evaluation = self.authorization.evaluate(
                strategy_id=strategy_id,
                mode=current_mode,
                account_id=str(row.account_id),
                environment=str(row.execution_environment),
                version_id=str(row.version_id),
                source_sha256=str(row.source_sha256),
                policy_hash=policy_hash,
                grant_id=str(row.grant_id) if row.grant_id else None,
                now=moment,
                # Same transaction as the claim: the authority decision and the
                # claim commit together under one strategy lock.
                session=session,
            )
            if not evaluation.get("authorized"):
                self._refuse_claim(
                    session,
                    row,
                    str(evaluation.get("refusal_code") or "GRANT_REQUIRED"),
                    {
                        "grant_id": row.grant_id,
                        "checked_at": moment.isoformat(),
                        "stage": "dispatch_claim",
                    },
                    moment,
                )
                return None
        elif row.decision_kind != "manual":
            self._refuse_claim(
                session,
                row,
                "AWAITING_OWNER_APPROVAL",
                {"checked_at": moment.isoformat()},
                moment,
            )
            return None

        claim_id = str(uuid.uuid4())
        row.status = "dispatching"
        row.dispatch_claim_id = claim_id
        row.dispatch_claimed_at = moment
        row.dispatch_started_at = moment
        row.updated_at = moment
        session.add(self._audit(row, "claimed", claim_id, "system", moment))
        return self._view(row)

    def _refuse_claim(
        self,
        session: Any,
        row: HostedExecutionRequest,
        code: str,
        detail: Mapping[str, Any],
        moment: datetime,
    ) -> None:
        row.status = "refused"
        row.refusal_code = str(code)
        row.refusal_detail = dict(detail)
        row.updated_at = moment
        session.add(self._audit(row, "refused", str(code), "system", moment))

    @staticmethod
    def _result_outcome(result: Mapping[str, Any]) -> Dict[str, Any]:
        """What the executor's own report MEANS, without overstating it.

        A dispatch is not a fill and not settlement. The request records that the
        work was dispatched, and carries the executor's own outcome word
        (``submitted`` / ``filled`` / ``rejected`` / ``failed`` / ``uncertain`` /
        ``no_op``) plus its per-step states so an operator sees what actually
        happened instead of a single optimistic "executed".

        * a submission happened and is in flight  -> ``executed`` + outcome word
        * the outcome is UNKNOWN (transport, failure after send) -> unresolved
        * an authoritative refusal / no-op -> ``refused`` with a named code
        """
        status = str(result.get("status") or "").lower()
        steps = list(result.get("steps") or [])
        step_states = [str(step.get("state") or "") for step in steps]
        outcome_state = status or (step_states[0] if step_states else "submitted")
        if status == "rejected" or (step_states and set(step_states) == {"rejected"}):
            return {"status": "refused", "refusal_code": "ORDER_REJECTED", "outcome_state": "rejected"}
        if status == "no_op" or (step_states and set(step_states) == {"no_op"}):
            return {"status": "refused", "refusal_code": "NO_OP", "outcome_state": "no_op"}
        if status in ("uncertain",) or "uncertain" in step_states:
            return {
                "status": "dispatch_unresolved",
                "refusal_code": "TRANSPORT_UNCERTAIN",
                "outcome_state": "uncertain",
            }
        if status == "failed" or "failed" in step_states:
            return {
                "status": "dispatch_unresolved",
                "refusal_code": "EXECUTION_OUTCOME_UNKNOWN",
                "outcome_state": "failed",
            }
        return {"status": "executed", "refusal_code": None, "outcome_state": outcome_state}

    def _preflight_refusal(
        self, request_id: str, *, claim_id: Optional[str], moment: datetime
    ) -> Optional[Dict[str, Any]]:
        """Re-derive the whole authority fence at the dispatch boundary.

        A batch can sit between the claim and the dispatch, so the attempt, the
        mode, the grant and the policy are re-read HERE under the strategy row
        lock - the same lock a revocation takes. A refusal is recorded with the
        claim CAS, so a stale worker cannot overwrite it later.
        """
        session = self.session_factory()
        try:
            row = session.execute(
                select(HostedExecutionRequest)
                .where(HostedExecutionRequest.request_id == str(request_id))
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise ExecutionRequestNotFound({"request_id": str(request_id)})
            if str(row.status) != "dispatching":
                raise ExecutionRequestStateError(
                    {
                        "request_id": str(request_id),
                        "status": str(row.status),
                        "message": "only a claimed request can be dispatched",
                    }
                )
            if claim_id is not None and str(row.dispatch_claim_id or "") != str(claim_id):
                raise ExecutionRequestStateError(
                    {
                        "request_id": str(request_id),
                        "claim_id": str(claim_id),
                        "current_claim_id": row.dispatch_claim_id,
                        "message": "this dispatch claim was superseded",
                    }
                )
            strategy = self.authorization.lock_strategy(session, str(row.strategy_id))
            if strategy is None:
                return {"refusal_code": "STRATEGY_NOT_FOUND", "detail": {}}
            refusal = self._attempt_refusal(session, row, moment)
            if refusal is not None:
                return {"refusal_code": refusal, "detail": {"stage": "dispatch_boundary"}}
            current_mode = str(strategy.authorization_mode or "approval_based")
            policy_hash = self.authorization.policy_hash_for(
                self.authorization.policy_snapshot(session, strategy)
            )
            if policy_hash != str(row.policy_hash):
                return {
                    "refusal_code": (
                        "GRANT_POLICY_CHANGED"
                        if str(row.authorization_mode) == "autonomous"
                        else "POLICY_CHANGED"
                    ),
                    "detail": {
                        "policy_hash": str(row.policy_hash),
                        "current_policy_hash": policy_hash,
                        "stage": "dispatch_boundary",
                    },
                }
            if str(row.authorization_mode) == "autonomous":
                evaluation = self.authorization.evaluate(
                    strategy_id=str(row.strategy_id),
                    mode=current_mode,
                    account_id=str(row.account_id),
                    environment=str(row.execution_environment),
                    version_id=str(row.version_id),
                    source_sha256=str(row.source_sha256),
                    policy_hash=policy_hash,
                    grant_id=str(row.grant_id) if row.grant_id else None,
                    now=moment,
                    session=session,
                )
                if not evaluation.get("authorized"):
                    return {
                        "refusal_code": str(evaluation.get("refusal_code") or "GRANT_REQUIRED"),
                        "detail": {"stage": "dispatch_boundary", "grant_id": row.grant_id},
                    }
            return None
        except ExecutionRequestError:
            session.rollback()
            raise
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    async def dispatch(
        self,
        request_id: str,
        *,
        claim_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Run one already-claimed request through the shared pipeline.

        The claim happened under the strategy lock; this method re-derives the
        fence at the dispatch boundary, then performs the admission, reservation,
        structural approval and execution, and records what the executor actually
        reported. Exactly one physical submission is possible per plan because the
        executors' own per-step claims and broker idempotency stay authoritative.
        """
        moment = now or self._clock()
        preflight = self._preflight_refusal(str(request_id), claim_id=claim_id, moment=moment)
        if preflight is not None:
            return self.finish(
                str(request_id),
                status="refused",
                refusal_code=str(preflight["refusal_code"]),
                detail=dict(preflight.get("detail") or {}),
                claim_id=claim_id,
                expected_status="dispatching",
                now=moment,
            )
        row = self.get(str(request_id))
        if row is None:
            raise ExecutionRequestNotFound({"request_id": str(request_id)})
        plan = self.pipeline.plan(str(row["plan_id"]))
        if plan is None:
            return self.finish(
                str(request_id),
                status="refused",
                refusal_code="PLAN_NOT_FOUND",
                detail={},
                claim_id=claim_id,
                expected_status="dispatching",
                now=moment,
            )

        environment = str(row["execution_environment"])
        actor = self._dispatch_actor(row)
        detail: Dict[str, Any] = {"environment": environment}
        try:
            reservation = self.pipeline.reservation_for_plan(str(row["plan_id"]))
            if reservation is None or str(reservation.get("status")) not in (
                "active",
                "renewed",
            ):
                reservation = self.pipeline.reserve(
                    plan, environment=environment, actor=actor
                )
            approval = self.pipeline.approve(
                plan,
                actor=actor,
                reservation_id=str(reservation["reservation_id"]),
                environment=environment,
                actor_kind="automatic" if str(row["authorization_mode"]) == "autonomous" else "manual",
                # S3: the approval is bound to the immutable version identity the
                # REQUEST pinned, so a version, source or policy change after the
                # request is a named refusal at release rather than a silent
                # re-interpretation of what the owner authorised.
                version_binding={
                    "strategy_version_id": str(row["version_id"]),
                    "version_number": row["version_number"],
                    "source_sha256": str(row["source_sha256"]),
                    "policy_hash": str(row["policy_hash"]),
                },
                evidence={
                    "authorization_mode": str(row["authorization_mode"]),
                    "grant_id": row["grant_id"],
                    "execution_request_id": str(row["request_id"]),
                    "execution_environment": environment,
                    "policy_hash": str(row["policy_hash"]),
                    "plan_id": str(row["plan_id"]),
                    "plan_hash": str(row["plan_hash"]),
                },
                reuse_existing=True,
            )
        except PipelineRefusal as exc:
            return self.finish(
                str(request_id),
                status="refused",
                refusal_code=exc.reason_code,
                detail={"stage": "preparation", **exc.detail},
                reservation_id=detail.get("reservation_id"),
                claim_id=claim_id,
                expected_status="dispatching",
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001 - an admission fault is a refusal
            return self.finish(
                str(request_id),
                status="refused",
                refusal_code=getattr(exc, "reason_code", "ADMISSION_REFUSED"),
                detail={"stage": "preparation", "message": str(exc)},
                claim_id=claim_id,
                expected_status="dispatching",
                now=moment,
            )

        detail["reservation_id"] = str(reservation["reservation_id"])
        detail["approval_id"] = str((approval or {}).get("approval_id") or "") or None
        try:
            result = await self.pipeline.execute(plan, actor=actor)
        except PipelineRefusal as exc:
            return self.finish(
                str(request_id),
                status="refused",
                refusal_code=exc.reason_code,
                detail={"stage": "execution", **exc.detail},
                reservation_id=detail["reservation_id"],
                approval_id=detail["approval_id"],
                claim_id=claim_id,
                expected_status="dispatching",
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001 - unknown outcomes stay unresolved
            return self.finish(
                str(request_id),
                status="dispatch_unresolved",
                refusal_code=str(getattr(exc, "reason_code", "EXECUTION_OUTCOME_UNKNOWN")),
                detail={"stage": "execution", "message": str(exc)},
                reservation_id=detail["reservation_id"],
                approval_id=detail["approval_id"],
                claim_id=claim_id,
                expected_status="dispatching",
                now=moment,
            )

        # The request records DISPATCH, never a fill or settlement: the executor's
        # own outcome word travels with it so "submitted", "rejected", "failed"
        # and "uncertain" stay distinguishable.
        outcome = self._result_outcome(result)
        outcome_detail = {
            **detail,
            "outcome_state": str(outcome["outcome_state"]),
            "result": result,
            "authorization_mode": str(row["authorization_mode"]),
            "grant_id": row["grant_id"],
            "decision_kind": row["decision_kind"],
            "note": (
                "the request was dispatched; fills, settlement and protection are "
                "tracked by the execution trail"
            ),
        }
        return self.finish(
            str(request_id),
            status=str(outcome["status"]),
            refusal_code=outcome["refusal_code"],
            detail=outcome_detail,
            reservation_id=detail["reservation_id"],
            approval_id=detail["approval_id"],
            claim_id=claim_id,
            expected_status="dispatching",
            now=moment,
        )

    def finish(
        self,
        request_id: str,
        *,
        status: str,
        refusal_code: Optional[str],
        detail: Mapping[str, Any],
        reservation_id: Optional[str] = None,
        approval_id: Optional[str] = None,
        claim_id: Optional[str] = None,
        expected_status: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Record a terminal outcome, CAS-fenced on the claim it belongs to.

        A stale dispatcher must never overwrite a decision it no longer owns: the
        update only lands when the row is still in ``expected_status`` and still
        carries ``claim_id``. A lost CAS raises, so the caller reports a stale
        finish instead of silently clobbering recovery's answer.
        """
        moment = now or self._clock()
        if status not in ("executed", "refused", "dispatch_unresolved"):
            raise ExecutionRequestInputError({"status": str(status)})
        session = self.session_factory()
        try:
            conditions = [HostedExecutionRequest.request_id == str(request_id)]
            if expected_status is not None:
                conditions.append(HostedExecutionRequest.status == str(expected_status))
            if claim_id is not None:
                conditions.append(HostedExecutionRequest.dispatch_claim_id == str(claim_id))
            values: Dict[str, Any] = {
                "status": str(status),
                "refusal_code": refusal_code,
                # A durable JSON column must carry JSON. Evidence assembled from
                # live objects can hold a ``datetime`` (a margin ``as_of``, a
                # publication stamp, an executor report); failing the write would
                # leave an UNKNOWN broker outcome unrecorded, which is the one
                # state this table exists to make unambiguous.
                "refusal_detail": (
                    {} if status == "executed" else _json_safe(dict(detail))
                ),
                "execution_detail": _json_safe(dict(detail)),
                "dispatch_finished_at": moment,
                "updated_at": moment,
            }
            if reservation_id:
                values["reservation_id"] = str(reservation_id)
            if approval_id:
                values["approval_id"] = str(approval_id)
            result = session.execute(
                update(HostedExecutionRequest).where(*conditions).values(**values)
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                session.rollback()
                current = self._read_state(str(request_id))
                if current is None:
                    raise ExecutionRequestNotFound({"request_id": str(request_id)})
                raise ExecutionRequestStateError(
                    {
                        "request_id": str(request_id),
                        "status": current["status"],
                        "dispatch_claim_id": current["dispatch_claim_id"],
                        "message": (
                            "this finish is stale: the request moved on "
                            "(another claim, a recovery decision or a terminal state) "
                            "and was not overwritten"
                        ),
                    }
                )
            row = self._load_or_raise(session, str(request_id))
            session.add(self._audit(row, str(status), str(refusal_code or ""), "system", moment))
            session.commit()
            return self._view(row)
        except ExecutionRequestError:
            session.rollback()
            raise
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    def recover_abandoned_claims(
        self,
        *,
        timeout_seconds: int = DEFAULT_CLAIM_TIMEOUT_SECONDS,
        limit: int = 25,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Inspect claims left ``dispatching`` by a dead or stalled dispatcher.

        Nothing is replayed. The plan's own durable records decide the outcome:

        * proof a submission reached the broker -> ``executed`` (dispatch done,
          fills/settlement still owned by the execution trail);
        * an AUTHORITATIVE post-send rejection -> ``refused`` (nothing accepted,
          and a replay cannot improve it);
        * anything else - including a claim that never left ``withheld`` or
          ``releasing``, and a ``no_op`` step - -> ``dispatch_unresolved`` and
          stays visible as pending work until an operator acts. Retrying would
          risk a second physical order, which the platform's unknown-submission
          rule forbids.
        """
        moment = now or self._clock()
        cutoff = moment - timedelta(seconds=int(timeout_seconds))
        counts = {"scanned": 0, "proved_submitted": 0, "proved_rejected": 0, "unresolved": 0}
        counts["stale"] = 0
        # Phase 1: read the candidates and CLOSE the session, so resolving a
        # claim never runs a second transaction on the same connection.
        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(HostedExecutionRequest)
                    .where(
                        HostedExecutionRequest.status == "dispatching",
                        HostedExecutionRequest.dispatch_claimed_at.is_not(None),
                        HostedExecutionRequest.dispatch_claimed_at < cutoff,
                    )
                    .order_by(HostedExecutionRequest.dispatch_claimed_at)
                    .limit(int(limit))
                )
                .scalars()
                .all()
            )
            candidates = [
                {
                    "request_id": str(row.request_id),
                    "plan_id": str(row.plan_id),
                    "claim_id": str(row.dispatch_claim_id or ""),
                    "claimed_at": row.dispatch_claimed_at,
                }
                for row in rows
            ]
            proofs_by_plan = {
                candidate["plan_id"]: self._submission_proof(session, candidate["plan_id"])
                for candidate in candidates
            }

        # Phase 2: resolve each candidate. The `finish` CAS is what lets a
        # concurrent dispatcher win cleanly instead of being overwritten.
        for candidate in candidates:
            counts["scanned"] += 1
            proofs = proofs_by_plan.get(candidate["plan_id"]) or {}
            outcome = str(proofs.get("outcome") or "unknown")
            if outcome == "submitted":
                status = "executed"
                code: Optional[str] = None
                detail: Dict[str, Any] = {
                    "recovered": True,
                    "outcome_state": "submitted",
                    "evidence": proofs,
                    "note": (
                        "the plan's own trail or live claim proves a submission "
                        "reached the broker; the claim was resolved rather than "
                        "replayed, and fills/settlement stay with the execution trail"
                    ),
                }
                counts["proved_submitted"] += 1
            elif outcome == "rejected":
                # An AUTHORITATIVE post-send refusal. Nothing was accepted, so
                # recording it as refused is honest - and it is still never
                # replayed, because a replay cannot restore a refused order.
                status = "refused"
                code = "ORDER_REJECTED"
                counts["proved_rejected"] = int(counts.get("proved_rejected") or 0) + 1
                detail = {
                    "recovered": True,
                    "outcome_state": "rejected",
                    "evidence": proofs,
                    "note": (
                        "the plan's own trail proves an authoritative rejection "
                        "after the send; no order was accepted and nothing is retried"
                    ),
                }
            else:
                status = "dispatch_unresolved"
                code = "DISPATCH_OUTCOME_UNKNOWN"
                claimed_at = candidate.get("claimed_at")
                if proofs.get("unsubmitted_claims") and not proofs.get("in_flight_claims"):
                    # The step was materialized but nothing proves it left the
                    # platform: a pre-send claim is not a submission.
                    outcome_state = "not_submitted"
                    note = (
                        "only a pre-send or withheld claim exists for this plan, so "
                        "no submission is proven; the claim is not replayed without "
                        "an operator decision"
                    )
                else:
                    outcome_state = "unknown"
                    note = (
                        "no durable evidence shows whether this plan's work reached "
                        "the broker, so the outcome stays unknown and is not retried "
                        "without evidence"
                    )
                detail = {
                    "recovered": True,
                    "outcome_state": outcome_state,
                    "message": note,
                    "evidence": proofs,
                    "plan_id": candidate["plan_id"],
                    "claimed_at": claimed_at.isoformat() if claimed_at else None,
                }
                counts["unresolved"] += 1
            try:
                self.finish(
                    candidate["request_id"],
                    status=status,
                    refusal_code=code,
                    detail=detail,
                    claim_id=candidate["claim_id"],
                    expected_status="dispatching",
                    now=moment,
                )
            except ExecutionRequestStateError:
                counts["stale"] += 1
        return counts

    # -- dependent-release authority ---------------------------------------

    def release_authority_check(self, plan: Mapping[str, Any]):
        """A callable that re-derives the release authority INSIDE a transaction.

        The live adapter calls this while it holds the canonical book lock, right
        before the ``withheld -> releasing`` CAS, so the authority decision and
        the dispatch claim commit together. A grant revocation (which takes the
        same hosted-strategy lock) therefore either wins - and the release is
        refused with nothing placed - or the claim already won, which is the
        honest boundary the audit records.
        """
        capture = dict(plan)

        def _check(session: Any = None) -> Optional[Dict[str, Any]]:
            # The authoritative call passes the RELEASE TRANSACTION's session (so
            # the decision commits with the claim). The pre-pass calls it without
            # one; that path opens its own read session rather than assuming the
            # caller already holds a transaction.
            if session is not None:
                return self._dependent_release_refusal(session, capture)
            with self.session_factory() as own_session:
                return self._dependent_release_refusal(own_session, capture)

        return _check

    def authorize_dependent_release(
        self, plan: Mapping[str, Any], *, now: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        """Re-check mode/grant/policy for a plan whose dependent step is releasing.

        Returns ``None`` when the release is authorised, otherwise a named
        refusal. Called by the live sequence pass on EVERY dependent release, so
        an initial request cannot hand unbounded approval to a step that is
        released much later.
        """
        moment = now or self._clock()
        with self.session_factory() as session:
            return self._dependent_release_refusal(session, plan, now=moment)

    def _governing_request(
        self, session: Any, plan_id: str
    ) -> Optional[HostedExecutionRequest]:
        """The governed request that made this plan executable, if any.

        Ordered newest-first across the states that can have dispatched work. The
        ORIGINAL request is what a dependent release is bound to: the strategy's
        CURRENT mode is not allowed to launder a revoked autonomous mandate, and
        its absence (an operator click or a pre-governance row) leaves the
        adapter's own approval pins as the gate.
        """
        return (
            session.execute(
                select(HostedExecutionRequest)
                .where(
                    HostedExecutionRequest.plan_id == str(plan_id),
                    HostedExecutionRequest.status.in_(GOVERNING_REQUEST_STATUSES),
                )
                .order_by(
                    HostedExecutionRequest.created_at.desc(),
                    HostedExecutionRequest.request_id,
                )
            )
            .scalars()
            .first()
        )

    def _dependent_release_refusal(
        self,
        session: Any,
        plan: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        moment = now or self._clock()
        plan_id = str(plan.get("plan_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        request = self._governing_request(session, plan_id)
        if request is None:
            # Not governed: the operator path (or a pre-Phase-2 row). The live
            # adapter still re-validates every approval pin, so no new authority
            # is created here.
            return None

        # The attempt must still own this work, whatever the mode says now.
        attempt_refusal = self._attempt_refusal(session, request, moment)
        if attempt_refusal is not None:
            return self._release_refusal(
                attempt_refusal,
                {
                    "plan_id": plan_id,
                    "request_id": str(request.request_id),
                    "attempt": request.attempt,
                    "lease_epoch": request.lease_epoch,
                    "message": "the attempt that requested this work no longer holds it",
                },
            )

        strategy = self.authorization.lock_strategy(session, strategy_id)
        if strategy is None:
            return self._release_refusal("STRATEGY_NOT_FOUND", {"plan_id": plan_id})
        current_mode = str(strategy.authorization_mode or "approval_based")
        policy_hash = self.authorization.policy_hash_for(
            self.authorization.policy_snapshot(session, strategy)
        )

        if str(request.authorization_mode) == "autonomous":
            # The ORIGINAL request's mode is what matters: an autonomous mandate
            # that was revoked, superseded, expired, re-versioned, re-policied or
            # simply switched to manual refuses the LATER release.
            if current_mode != "autonomous":
                return self._release_refusal(
                    "AUTHORIZATION_MODE_NOT_AUTONOMOUS",
                    {
                        "plan_id": plan_id,
                        "request_id": str(request.request_id),
                        "authorization_mode": current_mode,
                        "message": (
                            "this plan was requested autonomously; the strategy is no "
                            "longer in autonomous mode, so the dependent release is refused"
                        ),
                    },
                )
            if policy_hash != str(request.policy_hash):
                return self._release_refusal(
                    "GRANT_POLICY_CHANGED",
                    {
                        "plan_id": plan_id,
                        "policy_hash": str(request.policy_hash),
                        "current_policy_hash": policy_hash,
                    },
                )
            evaluation = self.authorization.evaluate(
                strategy_id=strategy_id,
                mode=current_mode,
                account_id=account_id,
                environment=str(request.execution_environment),
                version_id=str(request.version_id),
                source_sha256=str(request.source_sha256),
                policy_hash=policy_hash,
                grant_id=str(request.grant_id) if request.grant_id else None,
                now=moment,
                session=session,
            )
            if not evaluation.get("authorized"):
                return self._release_refusal(
                    str(evaluation.get("refusal_code") or "GRANT_REQUIRED"),
                    {"plan_id": plan_id, "grant_id": request.grant_id,
                     "request_id": str(request.request_id)},
                )
            return None

        # A governed APPROVAL-based request: the owner's manual decision is the
        # authority, and the live adapter re-validates its pins on every release.
        if str(request.decision_kind or "") != "manual":
            return self._release_refusal(
                "REQUEST_DECISION_MISSING",
                {"plan_id": plan_id, "request_id": str(request.request_id)},
            )
        return None

    @staticmethod
    def _release_refusal(code: str, detail: Mapping[str, Any]) -> Dict[str, Any]:
        return {"reason_code": str(code), **dict(detail)}

    # -- internals ----------------------------------------------------------

    def _read_state(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(HostedExecutionRequest).where(
                    HostedExecutionRequest.request_id == str(request_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "status": str(row.status),
                "dispatch_claim_id": row.dispatch_claim_id,
            }

    @staticmethod
    def _load_or_raise(session: Any, request_id: str) -> HostedExecutionRequest:
        row = session.execute(
            select(HostedExecutionRequest).where(
                HostedExecutionRequest.request_id == str(request_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ExecutionRequestNotFound({"request_id": str(request_id)})
        return row

    @staticmethod
    def _lock_request(
        session: Any, owner_id: str, strategy_id: str, request_id: str
    ) -> Optional[HostedExecutionRequest]:
        return session.execute(
            select(HostedExecutionRequest)
            .where(
                HostedExecutionRequest.request_id == str(request_id),
                HostedExecutionRequest.owner_id == str(owner_id),
                HostedExecutionRequest.strategy_id == str(strategy_id),
            )
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def _audit(
        row: HostedExecutionRequest,
        event: str,
        actor_id: str,
        actor_kind: str,
        moment: datetime,
    ) -> HostedExecutionAudit:
        return HostedExecutionAudit(
            owner_id=str(row.owner_id),
            strategy_id=str(row.strategy_id),
            subject_kind="request",
            subject_id=str(row.request_id),
            event=str(event),
            actor_id=str(actor_id or "system"),
            actor_kind=str(actor_kind),
            detail={
                "status": str(row.status),
                "plan_id": str(row.plan_id),
                "authorization_mode": str(row.authorization_mode),
                "grant_id": row.grant_id,
                "dispatch_claim_id": row.dispatch_claim_id,
            },
            created_at=moment,
        )

    def _dispatch_actor(self, row: Mapping[str, Any]) -> str:
        """Who the pipeline records as the acting authority, never invented."""
        if str(row.get("authorization_mode")) == "autonomous":
            grant = self.authorization.get_grant(str(row.get("grant_id") or ""))
            if grant is not None:
                return str(grant.get("issued_by") or row.get("decision_actor") or "")
        return str(row.get("decision_actor") or "").strip()

    # -- structural admission -----------------------------------------------

    @staticmethod
    def _option_structure_refusal(
        *,
        plan: Mapping[str, Any],
        strategy_id: str,
        account_id: str,
        execution_environment: str,
        session: Any,
    ) -> Optional[Dict[str, Any]]:
        """The named option structural refusal for this plan, or ``None``.

        The rules themselves live with the plan/run binding edge, one per frozen
        phase: ``assess_option_entry_admissibility`` for an ENTRY and
        ``assess_option_adjust_admissibility`` for an ADJUST. This only asks the
        one the plan's frozen phase selects, through the service's own session,
        so an option plan the platform would refuse at execution is refused BY
        NAME before the owner is ever asked to approve it. An unreadable
        discovery refuses (``OPTION_STRUCTURE_DISCOVERY_UNKNOWN``) exactly as it
        does at execution - never "no runs".
        """
        from backend.options.execution.plan_binding import (
            PlanBindingRefusal,
            assess_option_adjust_admissibility,
            assess_option_entry_admissibility,
            is_option_adjust_plan,
            is_option_entry_plan,
        )

        if is_option_entry_plan(plan):
            assess = assess_option_entry_admissibility
        elif is_option_adjust_plan(plan):
            assess = assess_option_adjust_admissibility
        else:
            # Every other plan kind, and every option EXIT, is untouched: an exit
            # closes work that exists rather than opening or mutating a structure.
            return None
        try:
            assess(
                plan,
                strategy_id=str(strategy_id),
                account_id=str(account_id),
                execution_environment=str(execution_environment),
                session=session,
            )
        except PlanBindingRefusal as exc:
            return {"reason_code": exc.reason_code, "detail": exc.detail}
        return None

    def _attempt_refusal(
        self, session: Any, row: HostedExecutionRequest, moment: datetime
    ) -> Optional[str]:
        """The originating attempt must still be the one that holds authority.

        Every component of the fence is re-read from the persisted job: the
        attempted run, its credential, the lease epoch, the lease itself and the
        attempt number. A replacement attempt (a new run, a rotated child token,
        a stolen lease or a re-claim) refuses the work, so a queued request can
        never be dispatched by an attempt that no longer owns it - and a stale
        worker cannot finish it either.
        """
        if not row.job_id:
            return "HOSTED_ATTEMPT_UNKNOWN"
        job = session.execute(
            select(StrategyJob).where(StrategyJob.id == str(row.job_id))
        ).scalar_one_or_none()
        if job is None:
            return "HOSTED_ATTEMPT_UNKNOWN"
        if str(job.desired_state or "") != "started":
            return "HOSTED_ATTEMPT_STOPPED"
        if str(job.status or "") not in ATTEMPT_AUTHORITY_STATUSES:
            return "HOSTED_ATTEMPT_FENCED"
        lease_until = _as_utc(job.lease_until)
        if lease_until is None or lease_until <= moment:
            return "HOSTED_LEASE_EXPIRED"
        if str(job.run_id or "") != str(row.strategy_run_id or ""):
            return "HOSTED_ATTEMPT_RUN_REPLACED"
        # The credential the request was created under must still be the job's
        # credential (a rotated child token is a different attempt). Rows written
        # before the column existed have no token recorded and fall back to the
        # run/epoch/attempt fence above.
        if row.token_id and str(job.token_id or "") != str(row.token_id):
            return "HOSTED_ATTEMPT_TOKEN_REPLACED"
        if row.lease_epoch is not None and int(job.lease_epoch or 0) != int(row.lease_epoch):
            return "HOSTED_ATTEMPT_EPOCH_REPLACED"
        if row.attempt is not None and int(job.attempt or 0) != int(row.attempt):
            return "HOSTED_ATTEMPT_REPLACED"
        return None

    @staticmethod
    def _submission_proof(session: Any, plan_id: str) -> Dict[str, Any]:
        """What the plan's own durable records say happened, and nothing more.

        A claim, a withheld step or a pre-send refusal is NOT proof that the
        broker was ever contacted, so none of them may be reported as
        "submitted". The plan's append-only trail and its live submission claims
        are the only evidence, and they are read into three honest outcomes:

        * ``submitted`` - a submission reached the wire: a plan trail event that
          only a real attempt produces (``submitted`` / ``filled`` /
          ``partially_filled`` / ``failed``), or a live claim holding a broker
          order id / accepted transport state.
        * ``rejected`` - an AUTHORITATIVE refusal carrying positive broker
          evidence (an order id, a rejection code, or a rejection message). A
          bare ``rejected`` event with none of those is a pre-send validation
          refusal (``_reservation_preconditions`` and its siblings record one
          before anything is sent), which PROVES no submission and therefore
          stays ``unknown`` here.
        * ``unknown`` - nothing durable proves either way: a ``withheld`` claim
          (materialized before any send), a claim abandoned mid-release, and a
          ``no_op`` step (no order was needed at all).

        Only the first two are evidence. ``unknown`` is returned so the caller
        records a non-retryable unresolved outcome instead of inventing a
        submission from a claim.
        """
        from backend.strategies.attribution_models import (
            LivePlanSubmission,
            StrategyPlanExecutionEvent,
        )

        events = (
            session.execute(
                select(
                    StrategyPlanExecutionEvent.id,
                    StrategyPlanExecutionEvent.event,
                    StrategyPlanExecutionEvent.refusal_reason,
                    StrategyPlanExecutionEvent.detail,
                ).where(StrategyPlanExecutionEvent.plan_id == str(plan_id))
            )
            .all()
        )
        submissions = (
            session.execute(
                select(
                    LivePlanSubmission.submission_id,
                    LivePlanSubmission.state,
                    LivePlanSubmission.broker_order_ids,
                    LivePlanSubmission.detail,
                ).where(LivePlanSubmission.plan_id == str(plan_id))
            )
            .all()
        )

        evidence: Dict[str, Any] = {}
        submitted_events: List[str] = []
        rejected_events: List[str] = []
        no_evidence_events: List[str] = []
        for row in events:
            event_id, event, _refusal_reason, _detail = row
            name = str(event or "")
            if name in SUBMISSION_PROOF_EVENTS:
                submitted_events.append(str(event_id))
            elif name == "rejected" and ExecutionRequestService._event_proves_broker_send(
                _refusal_reason, _detail
            ):
                # An authoritative, evidenced refusal POST-SEND: the outcome is
                # decided, though nothing at the broker was accepted.
                rejected_events.append(str(event_id))
            else:
                # ``no_op`` (no order was needed) and a bare pre-send ``rejected``
                # both prove that NOTHING was sent.
                no_evidence_events.append(str(event_id))

        sent_submissions: List[str] = []
        withheld_submissions: List[str] = []
        attempted_submissions: List[str] = []
        for row in submissions:
            submission_id, state, broker_order_ids, _detail = row
            if ExecutionRequestService._submission_claim_reached_broker(
                state, broker_order_ids
            ):
                sent_submissions.append(str(submission_id))
            elif str(state or "") in RELEASE_ATTEMPTED_CLAIM_STATES:
                # The platform was about to send when the claim was abandoned,
                # so a submission may or may not have reached the broker. That
                # is genuinely unknown, not "nothing was sent".
                attempted_submissions.append(str(submission_id))
            else:
                # ``withheld``: the row exists but the release never began, so
                # nothing was sent.
                withheld_submissions.append(str(submission_id))

        if submitted_events or sent_submissions:
            evidence["outcome"] = "submitted"
        elif rejected_events:
            evidence["outcome"] = "rejected"
        else:
            evidence["outcome"] = "unknown"

        if submitted_events:
            evidence["execution_events"] = submitted_events
        if rejected_events:
            evidence["rejected_events"] = rejected_events
        if no_evidence_events:
            evidence["non_proof_events"] = no_evidence_events
        if sent_submissions:
            evidence["live_submissions"] = sent_submissions
        if withheld_submissions:
            evidence["unsubmitted_claims"] = withheld_submissions
        if attempted_submissions:
            evidence["in_flight_claims"] = attempted_submissions
        return evidence

    @staticmethod
    def _event_proves_broker_send(refusal_reason: Any, detail: Any) -> bool:
        """Whether a ``rejected`` event carries positive broker evidence.

        The evidence must be something only a real send produces: an order id,
        an explicit rejection code, or the broker's own rejection message. A
        bare refusal reason alone is the shape of a pre-send validation refusal,
        so it is deliberately NOT enough.
        """
        payload = dict(detail or {}) if isinstance(detail, Mapping) else {}
        for key in ("broker_order_id", "order_id", "rejection_code", "reject_reason"):
            if payload.get(key) not in (None, "", []):
                return True
        if payload.get("broker_status") not in (None, ""):
            return True
        message = str(payload.get("message") or "")
        if message and str(refusal_reason or "").upper().startswith("BROKER_"):
            return True
        return False

    @staticmethod
    def _submission_claim_reached_broker(state: Any, broker_order_ids: Any) -> bool:
        """Whether one live claim proves its step was actually sent.

        A broker order id is unconditional proof. Otherwise the state has to be
        one that only follows a send; ``withheld`` and ``releasing`` do not,
        because the claim row exists before the platform calls the broker.
        """
        try:
            order_ids = list(broker_order_ids or [])
        except TypeError:
            order_ids = []
        if any(str(value).strip() for value in order_ids):
            return True
        return str(state or "") in SUBMITTED_CLAIM_STATES

    def _version_source(self, strategy_id: str, version_id: str) -> str:
        with self.session_factory() as session:
            from backend.strategies.models import HostedStrategyVersion

            return str(
                session.execute(
                    select(HostedStrategyVersion.source_sha256).where(
                        HostedStrategyVersion.id == str(version_id),
                        HostedStrategyVersion.strategy_id == str(strategy_id),
                    )
                ).scalar_one_or_none()
                or ""
            )

    def _version_number(self, strategy_id: str, version_id: str) -> Optional[int]:
        with self.session_factory() as session:
            from backend.strategies.models import HostedStrategyVersion

            value = session.execute(
                select(HostedStrategyVersion.version).where(
                    HostedStrategyVersion.id == str(version_id),
                    HostedStrategyVersion.strategy_id == str(strategy_id),
                )
            ).scalar_one_or_none()
            return int(value) if value is not None else None

    @staticmethod
    def _evaluation_id(job: StrategyJob) -> Optional[str]:
        identity = dict(getattr(job, "identity_json", None) or {})
        value = str(identity.get("evaluation_id") or "").strip()
        return value or None
