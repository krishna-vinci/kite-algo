"""The proposal store: evaluation identity, idempotency, conflict, frozen plan.

One evaluation identity creates **at most one** immutable envelope (D-1). Three
outcomes are possible for a submission, and each is journalled:

* **exact retry** — the same ``(strategy_id, evaluation_id)`` with an identical
  payload hash returns the existing envelope (``idempotent_retry``);
* **conflict** — the same identity with a *different* payload hash is refused
  (``PROPOSAL_EVALUATION_CONFLICT``) because the evaluation already decided
  something else;
* **new** — a fresh envelope, validated into a frozen plan or refused.

A validation refusal is terminal: the envelope is stored ``refused``, the
evaluation identity is spent, and a corrected submission needs a new
``evaluation_id``. The platform never invents one. Nothing here is a scheduler or
an admission decision — a frozen plan is inert in this phase and nothing consumes
it for trading.

Ordering inside :meth:`ProposalStore.submit` matters. Validation and compilation
run **before** the envelope insert, because the envelope is insert-only (D-2):
its ``status`` must be its final value the first time it is written, so there is
no "insert received, then update to validated" path. Everything the submission
produces — envelope, plan, journal trail — is written in ONE transaction, so a
compile failure leaves a refusal and no plan rather than a half-written decision.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import (
    StrategyPlan,
    StrategyProposal,
    StrategyProposalJournal,
)
from backend.strategies.compiler import (
    PinnedCatalogRead,
    ValidationRefusal,
    compile_resolved_plan,
    compute_plan_hash,
    plan_pin,
)

#: Evaluation kinds a client may assert (mirrors ``ck_proposals_evaluation_kind``).
EVALUATION_KINDS = ("scheduled_occurrence", "run_now")


class ProposalStoreError(Exception):
    """A malformed submission (nothing was written)."""


class ProposalConflict(Exception):
    """The evaluation identity was already spent on a different decision."""

    reason_code = "PROPOSAL_EVALUATION_CONFLICT"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """Verbatim-payload hash: what makes "same evaluation, different decision"
    decidable after the fact."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ProposalSubmission:
    """One evaluation's proposal, as the caller asserts it."""

    strategy_id: str
    account_id: str
    evaluation_id: str
    evaluation_kind: str
    strategy_run_id: str
    target_kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    job_id: Optional[str] = None


class ProposalStore:
    """Validation flow from a submission to a frozen plan."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        catalog: Optional[PinnedCatalogRead] = None,
        compiler: Any = None,
    ) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._catalog = catalog
        self._compiler = compiler

    # -- reads --------------------------------------------------------------

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyProposal).where(StrategyProposal.proposal_id == str(proposal_id))
            ).scalar_one_or_none()
            return self._proposal_view(row) if row is not None else None

    def find_by_evaluation(self, *, strategy_id: str, evaluation_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyProposal).where(
                    StrategyProposal.strategy_id == str(strategy_id),
                    StrategyProposal.evaluation_id == str(evaluation_id),
                )
            ).scalar_one_or_none()
            return self._proposal_view(row) if row is not None else None

    def plan_for_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyPlan).where(StrategyPlan.proposal_id == str(proposal_id))
            ).scalar_one_or_none()
            return self._plan_view(row) if row is not None else None

    def list_proposals(self, *, strategy_id: str, limit: int = 50) -> list:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyProposal)
                .where(StrategyProposal.strategy_id == str(strategy_id))
                .order_by(StrategyProposal.created_at.desc())
                .limit(int(limit))
            ).scalars().all()
            return [self._proposal_view(row) for row in rows]

    def journal(self, *, strategy_id: str, limit: int = 200) -> list:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyProposalJournal)
                .where(StrategyProposalJournal.strategy_id == str(strategy_id))
                .order_by(StrategyProposalJournal.created_at, StrategyProposalJournal.event)
                .limit(int(limit))
            ).scalars().all()
            return [
                {
                    "event": str(row.event),
                    "evaluation_id": row.evaluation_id,
                    "proposal_id": row.proposal_id,
                    "reason_code": row.reason_code,
                    "detail": dict(row.detail or {}),
                }
                for row in rows
            ]

    @staticmethod
    def _proposal_view(row: StrategyProposal) -> Dict[str, Any]:
        return {
            "proposal_id": str(row.proposal_id),
            "strategy_id": str(row.strategy_id),
            "account_id": str(row.account_id),
            "evaluation_id": str(row.evaluation_id),
            "evaluation_kind": str(row.evaluation_kind),
            "job_id": row.job_id,
            "strategy_run_id": str(row.strategy_run_id),
            "target_kind": str(row.target_kind),
            "payload": dict(row.payload or {}),
            "payload_sha256": str(row.payload_sha256),
            "status": str(row.status),
        }

    @staticmethod
    def _plan_view(row: StrategyPlan) -> Dict[str, Any]:
        return {
            "plan_id": str(row.plan_id),
            "proposal_id": str(row.proposal_id),
            "strategy_id": str(row.strategy_id),
            "account_id": str(row.account_id),
            "plan_kind": str(row.plan_kind),
            "plan_hash": str(row.plan_hash),
            "logical_plan": dict(row.logical_plan or {}),
            "resolved_plan": dict(row.resolved_plan or {}),
            "pinned_universe_revision_id": row.pinned_universe_revision_id,
            "pinned_member_hash": row.pinned_member_hash,
            "pinned_catalog_generation": str(row.pinned_catalog_generation),
        }

    # -- submission ---------------------------------------------------------

    def submit(self, submission: ProposalSubmission) -> Dict[str, Any]:
        """Validate one evaluation into a frozen plan (or a terminal refusal)."""
        self._validate_submission(submission)
        digest = payload_sha256(submission.payload)

        existing = self.find_by_evaluation(
            strategy_id=submission.strategy_id, evaluation_id=submission.evaluation_id
        )
        if existing is not None:
            return self._resolve_existing(existing, digest)

        # Pin + compile BEFORE the write: the envelope is insert-only, so its
        # status must be final on first write.
        refusal: Optional[ValidationRefusal] = None
        resolved_plan = None
        compiled = None
        try:
            pinned = self._pinned_read(submission)
            if self._compiler is not None:
                compiled = self._compiler.compile(submission.payload, pinned)
            else:
                compiled = compile_resolved_plan(submission.target_kind, submission.payload, pinned)
        except ValidationRefusal as exc:
            refusal = exc

        try:
            return self._write(
                submission, digest=digest, compiled=compiled, refusal=refusal
            )
        except IntegrityError:
            # Lost the race on UNIQUE (strategy_id, evaluation_id): the winner's
            # envelope is the truth, and this submission resolves against it as
            # idempotent-or-conflict — never as a duplicate.
            self._rollback_and_forget()
            existing = self.find_by_evaluation(
                strategy_id=submission.strategy_id, evaluation_id=submission.evaluation_id
            )
            if existing is None:
                raise
            return self._resolve_existing(existing, digest)

    def _rollback_and_forget(self) -> None:
        """No-op hook: ``_write`` closes its own session on failure."""
        return None

    def _validate_submission(self, submission: ProposalSubmission) -> None:
        if submission.evaluation_kind not in EVALUATION_KINDS:
            raise ProposalStoreError(f"unknown evaluation_kind: {submission.evaluation_kind}")
        if not str(submission.evaluation_id or "").strip():
            raise ProposalStoreError("evaluation_id is required")
        if not str(submission.strategy_id or "").strip():
            raise ProposalStoreError("strategy_id is required")
        if not str(submission.account_id or "").strip():
            raise ProposalStoreError("account_id is required")
        if not str(submission.strategy_run_id or "").strip():
            raise ProposalStoreError("strategy_run_id is required provenance")
        if submission.evaluation_kind == "scheduled_occurrence" and not submission.job_id:
            # Mirrors ck_proposals_scheduled_requires_job: a scheduled occurrence
            # always names the job that produced it.
            raise ProposalStoreError("a scheduled occurrence requires job_id")

    def _pinned_read(self, submission: ProposalSubmission) -> PinnedCatalogRead:
        if self._catalog is not None:
            return self._catalog
        generation = submission.payload.get("catalog_generation")
        return PinnedCatalogRead(
            self.session_factory, generation=str(generation) if generation else None
        )

    def _resolve_existing(self, existing: Mapping[str, Any], digest: str) -> Dict[str, Any]:
        if str(existing.get("payload_sha256") or "") != digest:
            self._journal(
                strategy_id=str(existing["strategy_id"]),
                evaluation_id=str(existing["evaluation_id"]),
                proposal_id=str(existing["proposal_id"]),
                event="conflict",
                reason_code=ProposalConflict.reason_code,
                detail={"existing_payload_sha256": str(existing.get("payload_sha256") or "")},
            )
            raise ProposalConflict(
                {
                    "proposal_id": str(existing["proposal_id"]),
                    "evaluation_id": str(existing["evaluation_id"]),
                    "message": (
                        "This evaluation already produced a different proposal. A new market "
                        "decision requires a new evaluation_id."
                    ),
                }
            )
        self._journal(
            strategy_id=str(existing["strategy_id"]),
            evaluation_id=str(existing["evaluation_id"]),
            proposal_id=str(existing["proposal_id"]),
            event="idempotent_retry",
        )
        return {
            "proposal_id": str(existing["proposal_id"]),
            "status": str(existing["status"]),
            "plan": self.plan_for_proposal(str(existing["proposal_id"]))
            if str(existing["status"]) == "validated"
            else None,
            "idempotent": True,
        }

    def _write(
        self,
        submission: ProposalSubmission,
        *,
        digest: str,
        compiled: Any,
        refusal: Optional[ValidationRefusal],
    ) -> Dict[str, Any]:
        proposal_id = str(uuid.uuid4())
        status = "refused" if refusal is not None else "validated"
        # Journal rows written in one transaction must stay ordered: `created_at`
        # is the only ordering evidence the schema carries, and a whole
        # transaction can land inside one second (SQLite's CURRENT_TIMESTAMP has
        # second precision). Stamping an explicit, increasing timestamp per event
        # keeps the trail readable without adding a column.
        base = datetime.now(timezone.utc)
        events = 0

        def _stamp() -> datetime:
            nonlocal events
            value = base + timedelta(microseconds=events)
            events += 1
            return value

        session = self.session_factory()
        try:
            session.add(
                StrategyProposal(
                    proposal_id=proposal_id,
                    strategy_id=submission.strategy_id,
                    account_id=submission.account_id,
                    evaluation_id=submission.evaluation_id,
                    evaluation_kind=submission.evaluation_kind,
                    job_id=submission.job_id,
                    strategy_run_id=submission.strategy_run_id,
                    target_kind=submission.target_kind,
                    payload=dict(submission.payload),
                    payload_sha256=digest,
                    status=status,
                )
            )
            session.add(
                StrategyProposalJournal(
                    id=str(uuid.uuid4()),
                    strategy_id=submission.strategy_id,
                    evaluation_id=submission.evaluation_id,
                    proposal_id=proposal_id,
                    event="received",
                    detail={"target_kind": submission.target_kind},
                    created_at=_stamp(),
                )
            )

            plan_view: Optional[Dict[str, Any]] = None
            # The envelope must reach the database before the plan that
            # references it. Flushing here makes that ordering explicit rather
            # than depending on the unit of work's table sort — still ONE
            # transaction, so a later failure still leaves no plan behind.
            session.flush()
            if refusal is not None:
                session.add(
                    StrategyProposalJournal(
                        id=str(uuid.uuid4()),
                        strategy_id=submission.strategy_id,
                        evaluation_id=submission.evaluation_id,
                        proposal_id=proposal_id,
                        event="validation_refused",
                        reason_code=refusal.reason_code,
                        detail=dict(refusal.detail),
                        created_at=_stamp(),
                    )
                )
            else:
                pin = plan_pin(
                    pinned_catalog_generation=compiled.resolved["catalog_generation"],
                    pinned_universe_revision_id=compiled.universe_revision_id,
                    pinned_member_hash=compiled.member_hash,
                )
                plan_hash = compute_plan_hash(
                    logical=compiled.logical, resolved=compiled.resolved, pin=pin
                )
                plan_id = str(uuid.uuid4())
                session.add(
                    StrategyPlan(
                        plan_id=plan_id,
                        proposal_id=proposal_id,
                        strategy_id=submission.strategy_id,
                        account_id=submission.account_id,
                        plan_kind=compiled.target_kind,
                        plan_hash=plan_hash,
                        logical_plan=dict(compiled.logical),
                        resolved_plan=dict(compiled.resolved),
                        pinned_universe_revision_id=compiled.universe_revision_id,
                        pinned_member_hash=compiled.member_hash,
                        pinned_catalog_generation=str(compiled.resolved["catalog_generation"]),
                    )
                )
                session.add(
                    StrategyProposalJournal(
                        id=str(uuid.uuid4()),
                        strategy_id=submission.strategy_id,
                        evaluation_id=submission.evaluation_id,
                        proposal_id=proposal_id,
                        event="plan_created",
                        detail={"plan_hash": plan_hash},
                        created_at=_stamp(),
                    )
                )
                plan_view = {
                    "plan_id": plan_id,
                    "proposal_id": proposal_id,
                    "strategy_id": submission.strategy_id,
                    "account_id": submission.account_id,
                    "plan_kind": compiled.target_kind,
                    "plan_hash": plan_hash,
                    "logical_plan": dict(compiled.logical),
                    "resolved_plan": dict(compiled.resolved),
                    "pinned_universe_revision_id": compiled.universe_revision_id,
                    "pinned_member_hash": compiled.member_hash,
                    "pinned_catalog_generation": str(compiled.resolved["catalog_generation"]),
                }

            session.commit()
            result: Dict[str, Any] = {
                "proposal_id": proposal_id,
                "status": status,
                "plan": plan_view,
                "idempotent": False,
            }
            if refusal is not None:
                result["refusal"] = refusal.as_detail()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _journal(
        self,
        *,
        strategy_id: str,
        event: str,
        evaluation_id: Optional[str] = None,
        proposal_id: Optional[str] = None,
        reason_code: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        session = self.session_factory()
        try:
            session.add(
                StrategyProposalJournal(
                    id=str(uuid.uuid4()),
                    strategy_id=strategy_id,
                    evaluation_id=evaluation_id,
                    proposal_id=proposal_id,
                    event=event,
                    reason_code=reason_code,
                    detail=dict(detail or {}),
                    created_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
        except SQLAlchemyError:
            session.rollback()
        finally:
            session.close()


def plan_invalidation_state(
    plan: Mapping[str, Any],
    *,
    session_factory: Optional[Callable[[], Any]] = None,
    catalog: Optional[PinnedCatalogRead] = None,
) -> Dict[str, Any]:
    """Whether a frozen plan is still resolvable against the current catalog (D-5).

    **Derived, never stored.** A plan is an immutable fact and is never rewritten
    when the catalog moves; what changes is the answer to "does this plan still
    mean what it meant". The rule is deliberately narrow, because invalidating on
    any generation change would make every plan hostage to unrelated listings:

    * the pinned generation is still the current one → ``valid``;
    * a newer generation exists and **re-maps a pinned instrument's coordinate to
      a different instrument**, or its record is no longer ``active``, or its
      record is gone → ``invalidated``;
    * a newer generation that changes nothing about the pinned instruments →
      ``valid``. An unrelated catalog update must not invalidate a plan.

    Nothing here mutates or deletes the plan: superseded plans stay queryable.
    """
    pinned_generation = str(plan.get("pinned_catalog_generation") or "")
    factory = session_factory
    read = catalog or PinnedCatalogRead(factory)
    current = read.current_published_generation()

    base = {
        "pinned_catalog_generation": pinned_generation,
        "current_catalog_generation": current,
    }
    if not current or str(current) == pinned_generation:
        return {**base, "state": "valid", "reason": "CATALOG_GENERATION_CURRENT"}

    # Compare against the newest content — the question is about now, not the pin.
    latest = PinnedCatalogRead(factory, generation=str(current))
    try:
        latest.pin()
    except ValidationRefusal:
        return {**base, "state": "valid", "reason": "CATALOG_GENERATION_CURRENT"}

    relevant: list = []
    for leg in plan.get("resolved_plan", {}).get("legs", []):
        instrument_id = str(leg.get("instrument_id") or "")
        if not instrument_id:
            continue
        now = latest.resolve_symbol(
            str(leg.get("broker_exchange") or ""), str(leg.get("broker_symbol") or "")
        )
        if now is None:
            relevant.append({"instrument_id": instrument_id, "reason": "COORDINATE_UNMAPPED"})
            continue
        if str(now["instrument_id"]) != instrument_id:
            relevant.append(
                {
                    "instrument_id": instrument_id,
                    "reason": "COORDINATE_REMAPPED",
                    "current_instrument_id": str(now["instrument_id"]),
                }
            )
            continue
        lifecycle = latest.lifecycle(instrument_id)
        if lifecycle is None:
            relevant.append({"instrument_id": instrument_id, "reason": "INSTRUMENT_RECORD_MISSING"})
        elif lifecycle != "active":
            relevant.append(
                {
                    "instrument_id": instrument_id,
                    "reason": "INSTRUMENT_NOT_ACTIVE",
                    "lifecycle_status": lifecycle,
                }
            )

    if relevant:
        return {
            **base,
            "state": "invalidated",
            "reason": "PINNED_INSTRUMENT_CHANGED",
            "changes": relevant,
        }
    return {**base, "state": "valid", "reason": "CATALOG_GENERATION_UNRELATED"}
