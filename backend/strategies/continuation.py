"""Evaluation continuation: a distinct server-derived verdict over a FINITE
evaluation that finished and left a correctly attributed book.

This module is deliberately **not** a settlement verdict and it is deliberately
not a relaxation of one. ``backend.strategies.settlement`` keeps its exact
meaning: an open strategy book is ``unsettled`` in the four-axis rollup, and
``trading_settled_flat`` still requires flatness. Continuation answers a
different question:

    may the host clear this attempt's replacement block so the NEXT evaluation
    reads the SAME durable book, without anybody clicking "reconcile"?

The invariants that make that safe:

* the attempt is a **finite** evaluation that reported a **normal exit**
  (crashed / stopped / timed-out / uncertain attempts are never silently
  cleared);
* the child process group is provably gone and the child credential is revoked;
* a **current, versioned** settlement-barrier proof shows no unresolved
  execution can still land (a late fill bumps the barrier version and invalidates
  the proof by arithmetic — the same rule the settlement barrier uses);
* the strategy's attributed book is **fresh and complete** (published at a
  known projection version);
* there is no unexplained reconciliation divergence, no outstanding
  discretionary evaluation/approval, and no active recovery action;
* the existing **protection owner's** continuity is established — an in-flight
  protective exit is a blocker, never something continuation clears.

An open book is reported as ``held``. It is never reported as flat or settled.
Nothing here releases a claim, closes a position or declares a structure closed
just because a Python process exited.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    StrategyPositionProjection,
    StrategyProjectionState,
    StrategyReconciliationState,
    StrategyReservation,
)
from backend.strategies.settlement import ExecutionBarrier

__all__ = [
    "CASE_CONTINUATION_BLOCKED",
    "CASE_CONTINUATION_ELIGIBLE",
    "COMPLETION_EXITED",
    "COMPLETION_STOP_REQUESTED",
    "COMPLETION_TIMEOUT",
    "COMPLETION_UNKNOWN",
    "CONTINUATION_BLOCKERS",
    "CONTINUATION_ELIGIBLE",
    "ContinuationAssessment",
    "ContinuationEvidence",
    "assess_continuation",
    "continuation_collector_from_job",
    "continuation_digest",
]

#: The two cases this assessment can produce. They are intentionally NOT any of
#: the reconciliation or settlement case names, so a continuation can never be
#: read as "settled" or "flat".
CASE_CONTINUATION_ELIGIBLE = "continuation_eligible"
CASE_CONTINUATION_BLOCKED = "continuation_blocked"

#: A normal, eligible continuation. Distinct from ``TRADING_SETTLED_FLAT`` and
#: from ``QUIESCENT``-style vocabulary on purpose.
CONTINUATION_ELIGIBLE = "CONTINUATION_ELIGIBLE"

#: Refusal vocabulary. Every refusal is named; there is no generic "not allowed".
CONTINUATION_NOT_BLOCKED = "CONTINUATION_NOT_BLOCKED"
CONTINUATION_NOT_FINITE = "CONTINUATION_NOT_FINITE"
CONTINUATION_NOT_NORMAL_COMPLETION = "CONTINUATION_NOT_NORMAL_COMPLETION"
CONTINUATION_NOT_TRADE_CAPABLE = "CONTINUATION_NOT_TRADE_CAPABLE"
CONTINUATION_EVIDENCE_UNAVAILABLE = "CONTINUATION_EVIDENCE_UNAVAILABLE"
CONTINUATION_PROCESS_CLEANUP_UNKNOWN = "CONTINUATION_PROCESS_CLEANUP_UNKNOWN"
CONTINUATION_PROCESS_CLEANUP_UNRESOLVED = "CONTINUATION_PROCESS_CLEANUP_UNRESOLVED"
CONTINUATION_AUTHORITY_ACTIVE = "CONTINUATION_AUTHORITY_ACTIVE"
CONTINUATION_AUTHORITY_UNCERTAIN = "CONTINUATION_AUTHORITY_UNCERTAIN"
CONTINUATION_WORK_OUTSTANDING = "CONTINUATION_WORK_OUTSTANDING"
CONTINUATION_WORK_UNKNOWN = "CONTINUATION_WORK_UNKNOWN"
CONTINUATION_QUIESCENCE_UNVERIFIED = "CONTINUATION_QUIESCENCE_UNVERIFIED"
CONTINUATION_BOOK_UNREADABLE = "CONTINUATION_BOOK_UNREADABLE"
CONTINUATION_BOOK_NOT_PUBLISHED = "CONTINUATION_BOOK_NOT_PUBLISHED"
CONTINUATION_BOOK_INCOMPLETE = "CONTINUATION_BOOK_INCOMPLETE"
CONTINUATION_RECONCILIATION_DIVERGENCE = "CONTINUATION_RECONCILIATION_DIVERGENCE"
CONTINUATION_APPROVAL_OUTSTANDING = "CONTINUATION_APPROVAL_OUTSTANDING"
CONTINUATION_RECOVERY_ACTION_PENDING = "CONTINUATION_RECOVERY_ACTION_PENDING"
CONTINUATION_PROTECTION_IN_FLIGHT = "CONTINUATION_PROTECTION_IN_FLIGHT"
CONTINUATION_PROTECTION_UNKNOWN = "CONTINUATION_PROTECTION_UNKNOWN"
CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED = (
    "CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED"
)
CONTINUATION_EXPOSURE_UNKNOWN = "CONTINUATION_EXPOSURE_UNKNOWN"
CONTINUATION_OPTION_WORK_OUTSTANDING = "CONTINUATION_OPTION_WORK_OUTSTANDING"
CONTINUATION_OPTION_WORK_UNKNOWN = "CONTINUATION_OPTION_WORK_UNKNOWN"

CONTINUATION_BLOCKERS = (
    CONTINUATION_NOT_BLOCKED,
    CONTINUATION_NOT_FINITE,
    CONTINUATION_NOT_NORMAL_COMPLETION,
    CONTINUATION_NOT_TRADE_CAPABLE,
    CONTINUATION_EVIDENCE_UNAVAILABLE,
    CONTINUATION_PROCESS_CLEANUP_UNKNOWN,
    CONTINUATION_PROCESS_CLEANUP_UNRESOLVED,
    CONTINUATION_AUTHORITY_ACTIVE,
    CONTINUATION_AUTHORITY_UNCERTAIN,
    CONTINUATION_WORK_OUTSTANDING,
    CONTINUATION_WORK_UNKNOWN,
    CONTINUATION_QUIESCENCE_UNVERIFIED,
    CONTINUATION_BOOK_UNREADABLE,
    CONTINUATION_BOOK_NOT_PUBLISHED,
    CONTINUATION_BOOK_INCOMPLETE,
    CONTINUATION_RECONCILIATION_DIVERGENCE,
    CONTINUATION_APPROVAL_OUTSTANDING,
    CONTINUATION_RECOVERY_ACTION_PENDING,
    CONTINUATION_PROTECTION_IN_FLIGHT,
    CONTINUATION_PROTECTION_UNKNOWN,
    CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED,
    CONTINUATION_EXPOSURE_UNKNOWN,
    CONTINUATION_OPTION_WORK_OUTSTANDING,
    CONTINUATION_OPTION_WORK_UNKNOWN,
)

#: How the supervised child ended. ``exited`` is the only shape continuation
#: may clear; everything else (including an unknown/absent report) is a blocker.
COMPLETION_EXITED = "exited"
COMPLETION_STOP_REQUESTED = "stop_requested"
COMPLETION_TIMEOUT = "timeout"
COMPLETION_UNKNOWN = "unknown"

_TERMINAL_CLEANUP = "confirmed"
_AUTHORITY_REVOKED = "revoked"
_SETTLED_WORK = ("none", "settled")
_TERMINAL_REQUEST_STATUSES = ("executed", "refused", "rejected", "dispatch_unresolved")

#: Option-run statuses that mean the structure is FINISHED. Everything else -
#: including a status outside the durable vocabulary - is work.
_OPTION_RUN_FINISHED = frozenset({"exited", "settled"})

#: The only option-run status that holds a structure cleanly.
_OPTION_RUN_HELD = "entered"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass
class ContinuationEvidence:
    """The pinned identity and the observed axes continuation decides on.

    The identity half is what the proof is pinned to (owner, canonical/hosted
    strategy, account, environment, predecessor attempt/epoch/run, barrier
    version, projection version). The observed half mirrors the reconciliation
    collector's axes so the two never disagree about cleanup, authority, work,
    exposure or protection.
    """

    # -- pinned identity ---------------------------------------------------
    job_id: str
    strategy_id: str
    owner_id: str = ""
    account_id: str = ""
    execution_environment: str = "paper"
    attempt: int = 1
    lease_epoch: int = 0
    run_id: Optional[str] = None
    job_kind: str = "finite"
    job_status: str = "recovery_required"
    reconciled: bool = False
    desired_state: str = "started"
    barrier_version: Optional[int] = None
    projection_version: Optional[int] = None

    # -- observed axes -----------------------------------------------------
    completion_state: str = COMPLETION_UNKNOWN
    #: The runner-observed process exit code. Only ``0`` is a clean finite exit;
    #: ``None`` (unobserved) and any non-zero/signalled value are not.
    exit_code: Optional[int] = None
    trade_capable: bool = True
    process_cleanup_state: Optional[str] = None
    authority_state: str = "uncertain"  # revoked | active | uncertain
    work_state: str = "unknown"  # none | settled | outstanding | unknown
    exposure_state: str = "unknown"  # not_applicable | flat | open | unknown
    protection_state: str = "unknown"  # settled | active | unknown
    #: Whether the predecessor run carries a standing protection policy. When it
    #: does, the continuation must NOT close the run (closing it silently
    #: disables ``list_protection_enabled_runs``), so the protection owner keeps
    #: its policy across the handover.
    protection_enabled: bool = False
    recovery_action_required: bool = False
    quiescence_state: str = "unverified"  # verified | unverified
    book_state: str = "unknown"  # published | not_published | unreadable
    divergence_state: str = "none"  # none | divergence | unknown
    approval_state: str = "none"  # none | outstanding | unknown
    #: The OPTIONS lane keeps its OWN durable book, and the equity projection
    #: does not see it. ``none | held | outstanding | unknown``: a structure that
    #: is still being opened or closed, is only partially filled, needs cleanup,
    #: or owns an unresolved protective stage is outstanding work; only a
    #: cleanly held structure may continue, and it stays HELD.
    option_work_state: str = "unknown"
    #: One ``option_run_id=state`` entry per run this strategy owns, sorted so
    #: the digest is stable. Evidence for the proof, never an input to the axes.
    option_runs: List[str] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def held(self) -> bool:
        """An open book - or a held option structure - is never flat/settled."""
        return self.exposure_state == "open" or self.option_work_state == "held"

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["held"] = self.held
        return payload


@dataclass
class ContinuationAssessment:
    allowed: bool
    case: str
    reason_code: str
    blocking_reasons: List[str]
    held: bool
    notes: List[str]
    proof: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def continuation_digest(evidence: ContinuationEvidence) -> str:
    """A stable digest of the axes that gate continuation.

    Re-computed immediately before the unblock commits: a change means cleanup,
    authority, work, the book or protection moved under the caller, so the
    continuation fails closed.

    Deliberately excludes ``quiescence_state``: recording the proof is part of
    THIS operation, so its validity legitimately changes between the first
    collection and the recheck. The ``barrier_version`` it is pinned to IS in the
    digest, so a work event landing under us still fails the recheck closed.
    """
    canonical = {
        "job_id": evidence.job_id,
        "attempt": evidence.attempt,
        "lease_epoch": evidence.lease_epoch,
        "run_id": evidence.run_id,
        "job_kind": evidence.job_kind,
        "job_status": evidence.job_status,
        "reconciled": evidence.reconciled,
        "desired_state": evidence.desired_state,
        "barrier_version": evidence.barrier_version,
        "projection_version": evidence.projection_version,
        "completion_state": evidence.completion_state,
        "exit_code": evidence.exit_code,
        "trade_capable": evidence.trade_capable,
        "process_cleanup_state": evidence.process_cleanup_state,
        "authority_state": evidence.authority_state,
        "work_state": evidence.work_state,
        "exposure_state": evidence.exposure_state,
        "protection_state": evidence.protection_state,
        "protection_enabled": evidence.protection_enabled,
        "recovery_action_required": evidence.recovery_action_required,
        "book_state": evidence.book_state,
        "divergence_state": evidence.divergence_state,
        "approval_state": evidence.approval_state,
        "option_work_state": evidence.option_work_state,
        "option_runs": sorted(evidence.option_runs),
        "unavailable": sorted(evidence.unavailable),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _proof(evidence: ContinuationEvidence) -> Dict[str, Any]:
    """The pinned proof persisted with the audit row.

    Every field is a value the server derived; none is caller-supplied.
    """
    return {
        "kind": "evaluation_continuation",
        "owner_id": evidence.owner_id,
        "strategy_id": evidence.strategy_id,
        "account_id": evidence.account_id,
        "execution_environment": evidence.execution_environment,
        "predecessor": {
            "job_id": evidence.job_id,
            "attempt": int(evidence.attempt),
            "lease_epoch": int(evidence.lease_epoch),
            "run_id": evidence.run_id,
        },
        "barrier_version": evidence.barrier_version,
        "projection_version": evidence.projection_version,
        "book_state": evidence.book_state,
        "exposure_state": evidence.exposure_state,
        "held": evidence.held,
        "option_work_state": evidence.option_work_state,
        "option_runs": sorted(evidence.option_runs),
        "completion_state": evidence.completion_state,
        "exit_code": evidence.exit_code,
        "desired_state": evidence.desired_state,
        "protection_enabled": evidence.protection_enabled,
        "evidence_digest": continuation_digest(evidence),
    }


def assess_continuation(evidence: ContinuationEvidence) -> ContinuationAssessment:
    """Classify a finished finite attempt. Fails closed on anything unknown.

    The order is deliberate: identity/finality first, then the evidence axes,
    then the book. The first refusal wins so an operator always sees one named
    reason.
    """
    notes = list(evidence.notes)
    proof = _proof(evidence)

    def blocked(reason: str, extra: Optional[List[str]] = None) -> ContinuationAssessment:
        reasons = [reason] + list(extra or [])
        # De-duplicate while keeping the first (most specific) reason first.
        reasons = list(dict.fromkeys(reasons))
        return ContinuationAssessment(
            allowed=False,
            case=CASE_CONTINUATION_BLOCKED,
            reason_code=reasons[0],
            blocking_reasons=reasons,
            held=evidence.held,
            notes=notes,
            proof=proof,
        )

    # 1. There must actually be a blocked, unreconciled attempt to continue.
    if evidence.job_status != "recovery_required" or evidence.reconciled:
        return blocked(CONTINUATION_NOT_BLOCKED)

    # 2. Only a FINITE evaluation continues. A continuous job is not "a finished
    #    finite run", and treating it as one would clear work that is still live.
    if str(evidence.job_kind or "") != "finite":
        return blocked(CONTINUATION_NOT_FINITE)

    # 3. Only a NORMAL exit clears: the runner must have reported ``exited`` AND
    #    the trusted process exit code must be exactly 0. A crash, a signal, an
    #    unobserved exit, a stop, a timeout or any non-zero code keeps the block.
    if evidence.completion_state != COMPLETION_EXITED or evidence.exit_code != 0:
        return blocked(CONTINUATION_NOT_NORMAL_COMPLETION)

    # 3b. A stop race is the same shape as a stop: the operator asked this
    #     attempt to stop, so its clean exit is not an unattended completion.
    if str(evidence.desired_state or "started") != "started":
        return blocked(CONTINUATION_NOT_NORMAL_COMPLETION)

    # 4. A data-only attempt holds no book; the ordinary reconciliation path
    #    (or the unlaunched release path) decides those.
    if not evidence.trade_capable:
        return blocked(CONTINUATION_NOT_TRADE_CAPABLE)

    # 5. Nothing that could not be read may be assumed clean.
    if evidence.unavailable:
        return blocked(
            CONTINUATION_EVIDENCE_UNAVAILABLE,
            [f"unavailable:{item}" for item in sorted(evidence.unavailable)],
        )

    # 6. Process cleanup.
    if evidence.process_cleanup_state == "unresolved":
        return blocked(CONTINUATION_PROCESS_CLEANUP_UNRESOLVED)
    if evidence.process_cleanup_state != _TERMINAL_CLEANUP:
        return blocked(CONTINUATION_PROCESS_CLEANUP_UNKNOWN)

    # 7. Child authority.
    if evidence.authority_state == "active":
        return blocked(CONTINUATION_AUTHORITY_ACTIVE)
    if evidence.authority_state != _AUTHORITY_REVOKED:
        return blocked(CONTINUATION_AUTHORITY_UNCERTAIN)

    # 8. Unexplained reconciliation divergence on this book's coordinates is its
    #    own named refusal (it is also an in-flight item, but the specific reason
    #    is what the operator needs to see).
    if evidence.divergence_state == "divergence":
        return blocked(CONTINUATION_RECONCILIATION_DIVERGENCE)
    if evidence.divergence_state != "none":
        return blocked(CONTINUATION_RECONCILIATION_DIVERGENCE)

    # 9. Work that could still land.
    if evidence.work_state == "outstanding":
        return blocked(CONTINUATION_WORK_OUTSTANDING)
    if evidence.work_state not in _SETTLED_WORK:
        return blocked(CONTINUATION_WORK_UNKNOWN)

    # 9b. The OPTIONS lane's own durable book. An option structure writes no
    #     equity leg, so the attribution projection above is BLIND to it: a
    #     structure that is still entering (or partially entered), needs
    #     cleanup, is exiting (or partially exited), or owns an unresolved
    #     protective exit stage is outstanding work, and a successor must not be
    #     handed the block while that work is in flight. A cleanly held structure
    #     MAY continue - the successor reads the same durable run - but it stays
    #     ``held`` and is never reported flat or settled.
    if evidence.option_work_state == "outstanding":
        return blocked(CONTINUATION_OPTION_WORK_OUTSTANDING)
    if evidence.option_work_state not in ("none", "held"):
        return blocked(CONTINUATION_OPTION_WORK_UNKNOWN)

    # 10. Recovery action owned by the runtime (an unfinished protective/repair
    #    action) is a blocker, never something continuation clears.
    if evidence.recovery_action_required:
        return blocked(CONTINUATION_RECOVERY_ACTION_PENDING)

    # 11. Protection continuity. An in-flight protective exit blocks. An unknown
    #     protection owner is NOT silently treated as absent.
    if evidence.protection_state == "active":
        return blocked(CONTINUATION_PROTECTION_IN_FLIGHT)
    if evidence.protection_state != "settled":
        return blocked(CONTINUATION_PROTECTION_UNKNOWN)

    # 11b. Standing protection ownership is RUN-scoped, not strategy-scoped: the
    #      protection machinery only covers a run whose own ``status`` is
    #      ``open`` (see ``_list_protection_enabled_runs`` / the protection
    #      reader's ``RUN_NOT_OPEN`` gate), and each run carries its OWN pinned
    #      policy. Handing a held book to a successor run would therefore leave
    #      TWO protection owners over overlapping positions - a stale policy on
    #      the predecessor and a new one on the successor - and no supported
    #      mechanism merges them. There is no safe automatic handover for a
    #      protected book today, so it is refused BY NAME and stays for explicit
    #      operator reconciliation. Protection is never disabled to make this
    #      pass.
    if evidence.protection_enabled:
        return blocked(CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED)

    # 12. Discretionary evaluation/approval still outstanding.
    if evidence.approval_state == "outstanding":
        return blocked(CONTINUATION_APPROVAL_OUTSTANDING)
    if evidence.approval_state != "none":
        return blocked(CONTINUATION_APPROVAL_OUTSTANDING)

    # 13. The barrier must be READABLE so a proof can be pinned to its version.
    #     Whether a proof is CURRENT is established by the service, which records
    #     one under the book lock and re-validates that exact version inside the
    #     unblock transaction - a pure assessment cannot record anything.
    if evidence.barrier_version is None:
        return blocked(CONTINUATION_QUIESCENCE_UNVERIFIED)

    # 14. Fresh, complete attributed book. An unpublished book is NOT flat.
    if evidence.book_state == "unreadable":
        return blocked(CONTINUATION_BOOK_UNREADABLE)
    if evidence.book_state == "incomplete":
        # Unresolved/raw attribution (or unfinished live truth) cannot be
        # re-adopted: the successor would size against a book nobody proved.
        return blocked(CONTINUATION_BOOK_INCOMPLETE)
    if evidence.book_state != "published" or evidence.projection_version is None:
        return blocked(CONTINUATION_BOOK_NOT_PUBLISHED)

    # 15. Exposure must be KNOWN. Open is fine (held); unknown is not.
    if evidence.exposure_state not in ("flat", "open"):
        return blocked(CONTINUATION_EXPOSURE_UNKNOWN)

    notes.append(
        "finite evaluation finished cleanly; book is "
        + ("held" if evidence.held else "flat")
    )
    if evidence.option_work_state == "held":
        notes.append(
            "a cleanly held option structure is carried forward as HELD; the "
            "options lane keeps its own book and is never reported flat"
        )
    return ContinuationAssessment(
        allowed=True,
        case=CASE_CONTINUATION_ELIGIBLE,
        reason_code=CONTINUATION_ELIGIBLE,
        blocking_reasons=[],
        held=evidence.held,
        notes=notes,
        proof=proof,
    )


# ---------------------------------------------------------------------------
# evidence collection (sync: the barrier, the runner tables and the strategy
# tables all live in the same database, so the scheduler and the router can
# both call this without an event loop)
# ---------------------------------------------------------------------------


class ContinuationCollector:
    """Read-only evidence for one successor decision.

    It reads the SAME sources the reconciliation collector uses for the shared
    axes and never writes. It is sync by construction so the shared Run
    now/scheduled-job path can finish a proof after a host restart, where an
    event loop may not be available.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        barrier: Optional[ExecutionBarrier] = None,
    ) -> None:
        self._session_factory = session_factory
        self._barrier = barrier or ExecutionBarrier(session_factory=session_factory)

    # -- helpers -----------------------------------------------------------

    def _token_status(self, session: Any, token_id: Optional[str]) -> Optional[str]:
        if not token_id:
            return None
        row = session.execute(
            text("SELECT status FROM public.algo_worker_tokens WHERE token_id = :token_id"),
            {"token_id": str(token_id)},
        ).fetchone()
        return None if row is None else str(row[0] or "")

    def _run_row(self, session: Any, run_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not run_id:
            return None
        row = session.execute(
            text(
                "SELECT status, runtime_state_json FROM public.algo_worker_runs "
                "WHERE strategy_run_id = :run_id"
            ),
            {"run_id": str(run_id)},
        ).fetchone()
        if row is None:
            return None
        state = row[1]
        if isinstance(state, str):
            try:
                state = json.loads(state or "{}")
            except ValueError:
                state = {}
        return {"status": str(row[0] or ""), "runtime_state": dict(state or {})}

    def _book_state(
        self, session: Any, *, account_id: str, strategy_id: str, environment: str
    ) -> Dict[str, Any]:
        """Completeness of the attributed book, and its open legs.

        Only the platform's OWN publication is accepted as evidence: the book
        must carry a ``strategy_projection_state`` row (written by
        ``recompute_publish`` on the normal publication/rebuild path). An
        unpublished book is not flat.

        Two things make a published book unusable for a handover:

        * an ``identity_kind='raw'`` fact — exposure the platform could not
          attribute to a canonical instrument. Unknown attribution must never be
          re-adopted, because the successor would size against a book nobody can
          reconcile;
        * for ``live``, an account ingest cycle that is not idle/complete — the
          book would be derived from truth the platform has not finished reading.
        """
        state = session.execute(
            select(StrategyProjectionState).where(
                StrategyProjectionState.account_id == account_id,
                StrategyProjectionState.strategy_id == strategy_id,
                StrategyProjectionState.execution_environment == environment,
            )
        ).scalar_one_or_none()
        if state is None or state.last_rebuild_at is None:
            return {"book_state": "not_published", "projection_version": None, "legs": []}
        rows = session.execute(
            select(
                StrategyPositionProjection.net_quantity,
                StrategyPositionProjection.identity_kind,
            ).where(
                StrategyPositionProjection.account_id == account_id,
                StrategyPositionProjection.strategy_id == strategy_id,
                StrategyPositionProjection.execution_environment == environment,
            )
        ).all()
        open_legs = 0
        exposure = "flat"
        unresolved = 0
        for quantity, identity_kind in rows:
            try:
                net = int(quantity or 0)
            except (TypeError, ValueError):
                return {"book_state": "unreadable", "projection_version": None, "legs": []}
            if str(identity_kind) == "raw":
                # An unresolved fact is never silently treated as attributed.
                unresolved += 1
                continue
            if net != 0:
                open_legs += 1
                exposure = "open"
        detail: Dict[str, Any] = {
            "book_state": "published",
            "projection_version": int(state.projection_version or 0),
            "legs": open_legs,
            "exposure_state": exposure,
            "unresolved_identity": unresolved,
        }
        if unresolved:
            detail["book_state"] = "incomplete"
            detail["reason"] = "unresolved_identity_facts"
            return detail
        if environment == "live":
            ingest = session.execute(
                text(
                    "SELECT status, last_complete_ingest_at FROM public.account_ingest_state "
                    "WHERE account_id = :account_id"
                ),
                {"account_id": account_id},
            ).mappings().first()
            detail["ingest_status"] = None if ingest is None else str(ingest["status"] or "")
            if ingest is None or ingest["last_complete_ingest_at"] is None or (
                str(ingest["status"] or "") != "idle"
            ):
                detail["book_state"] = "incomplete"
                detail["reason"] = "live_account_ingest_not_complete"
                return detail
        return {
            **detail,
        }

    def _divergence_state(
        self, session: Any, *, account_id: str, strategy_id: str, environment: str
    ) -> str:
        coords = session.execute(
            select(
                StrategyPositionProjection.instrument_token,
                StrategyPositionProjection.exchange,
                StrategyPositionProjection.tradingsymbol,
                StrategyPositionProjection.product,
            ).where(
                StrategyPositionProjection.account_id == account_id,
                StrategyPositionProjection.strategy_id == strategy_id,
                StrategyPositionProjection.execution_environment == environment,
            )
        ).all()
        if not coords:
            return "none"
        book = {(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in coords}
        rows = session.execute(
            select(StrategyReconciliationState).where(
                StrategyReconciliationState.account_id == account_id
            )
        ).scalars().all()
        for row in rows:
            coordinate = (
                int(row.instrument_token),
                str(row.exchange),
                str(row.tradingsymbol),
                str(row.product),
            )
            if coordinate in book and str(row.divergence_class) != "aligned":
                return "divergence"
        return "none"

    def _approval_state(
        self,
        session: Any,
        *,
        strategy_id: str,
        run_id: Optional[str],
    ) -> str:
        """Outstanding discretionary evaluation/approval for the predecessor.

        The authority is the DURABLE EXECUTION REQUEST for the predecessor run.
        A request that has not reached a terminal DECISION is unfinished work.

        Two deliberate exclusions:

        * a standing ``active`` approval is NOT unfinished work. An approval row
          records an authorization and may legitimately remain ``active`` after
          its plan executed, so refusing on it alone would block a healthy
          recurring strategy forever. What matters is whether an *actionable*
          request is still outstanding;
        * ``dispatch_unresolved`` is NOT terminal-safe. The platform classes it as
          a final request state, but the broker outcome is unknown, so a quiet
          proof must not be built on it.
        """
        _ = strategy_id
        if not run_id:
            return "none"
        row = session.execute(
            text(
                "SELECT status FROM public.hosted_execution_requests "
                "WHERE strategy_run_id = :run_id AND status NOT IN "
                "('executed', 'refused', 'rejected') "
                "LIMIT 1"
            ),
            {"run_id": str(run_id)},
        ).first()
        return "outstanding" if row is not None else "none"

    @staticmethod
    def _option_run_state(row: Mapping[str, Any]) -> str:
        """``held`` / ``finished`` / ``outstanding`` for ONE durable option run.

        Only ``entered`` counts as a cleanly held structure, and only when nothing
        about that run is still in flight: a partially filled leg, a failed leg,
        or an unresolved protective exit stage all keep it outstanding. A status
        outside the durable vocabulary is outstanding, never finished.
        """
        status = str(row.get("status") or "").strip().lower()
        if bool(row.get("protective_exit_unresolved")):
            return "outstanding"
        if status in _OPTION_RUN_FINISHED:
            return "finished"
        outstanding_legs = len(list(row.get("pending_legs") or [])) + len(
            list(row.get("failed_legs") or [])
        )
        if status == _OPTION_RUN_HELD and not outstanding_legs:
            return "held"
        return "outstanding"

    def _option_run_work(
        self,
        *,
        account_id: str,
        strategy_id: str,
        environment: str,
        session: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """This strategy's OWN option runs, reduced to the continuation axis.

        The read is the platform's scope-derived option-run discovery (the same
        one ``owned_work()`` uses), so a strategy cannot name another account or
        environment. Unknown or truncated coverage is reported as ``unknown``,
        which the assessment refuses - it is never read as "no structures".
        """
        from backend.strategies.execution_snapshot import OwnedWorkSnapshotService

        service = OwnedWorkSnapshotService(session_factory=self._session_factory)
        runs, coverage = service.option_runs_for_scope(
            account_id=str(account_id),
            strategy_id=str(strategy_id),
            environment=str(environment),
            session=session,
        )
        if str(coverage.get("coverage") or "unknown") != "known":
            return {
                "state": "unknown",
                "runs": [],
                "reason": str(coverage.get("reason") or "option_run_discovery_unknown"),
            }
        classified = [
            (
                str(row.get("option_run_id") or ""),
                self._option_run_state(row),
            )
            for row in list(runs or [])
        ]
        states = {state for _run_id, state in classified}
        if "outstanding" in states:
            aggregate = "outstanding"
        elif "held" in states:
            aggregate = "held"
        else:
            aggregate = "none"
        return {
            "state": aggregate,
            "runs": sorted(f"{run_id}={state}" for run_id, state in classified),
            "reason": "",
        }

    # -- the collection ----------------------------------------------------

    def collect(
        self,
        job: Any,
        *,
        completion_state: str = COMPLETION_UNKNOWN,
    ) -> ContinuationEvidence:
        unavailable: List[str] = []
        notes: List[str] = []

        # The PERSISTED report wins over the caller's: after a host restart the
        # caller has nothing to report, and the durable marker written by the
        # supervisor lifecycle API is the only honest evidence of how the child
        # ended.
        persisted_completion = getattr(job, "completion_state", None)
        if persisted_completion:
            completion_state = str(persisted_completion)

        account_id = str(getattr(job, "account_scope", "") or "")
        strategy_id = str(getattr(job, "strategy_id", "") or "")
        environment = str(getattr(job, "execution_mode", "") or "")
        run_id = getattr(job, "run_id", None)

        caps: Dict[str, Any] = {}
        try:
            from backend.strategies import service as strategy_service

            caps = strategy_service.parse_capability_snapshot(
                getattr(job, "capabilities_snapshot", None)
            )
            trade_capable = True if not caps else bool(caps.get("trade"))
        except Exception:  # noqa: BLE001 - ambiguous snapshot is treated as trading
            trade_capable = True
            notes.append("capabilities_ambiguous_treated_as_trading")

        barrier_version: Optional[int] = None
        quiescence_state = "unverified"
        try:
            state = self._barrier.state(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=environment,
            )
            barrier_version = int(state.get("barrier_version") or 0)
            quiescence_state = "verified" if bool(state.get("proof_valid")) else "unverified"
        except Exception:  # noqa: BLE001 - unreadable barrier is unverified
            unavailable.append("settlement_barrier")

        session = self._session_factory()
        try:
            # authority
            token_status: Optional[str] = None
            try:
                token_status = self._token_status(session, getattr(job, "token_id", None))
            except SQLAlchemyError:
                unavailable.append("algo_worker_tokens")
            terminal = str(getattr(job, "status", "") or "") in (
                "recovery_required",
                "stopped",
                "failed",
            )
            if "algo_worker_tokens" in unavailable:
                authority_state = "uncertain"
            elif token_status == "active":
                authority_state = "active"
            elif token_status == "revoked":
                authority_state = "revoked" if terminal else "uncertain"
            elif token_status is None and getattr(job, "token_id", None) is None:
                authority_state = "revoked"
            else:
                authority_state = "uncertain"

            # run + protection ownership
            protection_state = "unknown"
            protection_enabled = False
            recovery_action_required = False
            run_status: Optional[str] = None
            try:
                run = self._run_row(session, run_id)
            except SQLAlchemyError:
                run = None
                unavailable.append("algo_worker_runs")
            if run is not None:
                run_status = str(run["status"] or "")
                runtime_state = dict(run.get("runtime_state") or {})
                protection_enabled = bool(
                    dict(runtime_state.get("backend_protection") or {}).get("enabled")
                )
                protection = dict(runtime_state.get("backend_protection_state") or {})
                protection_state = "active" if protection.get("exit_submitted") else "settled"
                recovery = dict(runtime_state.get("runtime_recovery") or {})
                recovery_action_required = bool(recovery.get("action_required"))
            elif getattr(job, "handoff_at", None) is None:
                protection_state = "settled"

            # book
            try:
                book = self._book_state(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    environment=environment,
                )
            except SQLAlchemyError:
                book = {"book_state": "unreadable", "projection_version": None}
                unavailable.append("strategy_projection")
            exposure_state = str(book.get("exposure_state") or "unknown")
            if book.get("book_state") != "published":
                exposure_state = "unknown"

            # divergence / approval
            divergence_state = "unknown"
            try:
                divergence_state = self._divergence_state(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    environment=environment,
                )
            except SQLAlchemyError:
                unavailable.append("strategy_reconciliation_state")
            approval_state = "unknown"
            try:
                approval_state = self._approval_state(
                    session, strategy_id=strategy_id, run_id=run_id
                )
            except SQLAlchemyError:
                unavailable.append("strategy_approvals")

            # the OPTIONS lane's own durable book (a separate axis: the equity
            # projection below never sees an option structure)
            option_work: Dict[str, Any] = {
                "state": "unknown",
                "runs": [],
                "reason": "option_run_read_failed",
            }
            try:
                option_work = self._option_run_work(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    environment=environment,
                    session=session,
                )
            except Exception:  # noqa: BLE001 - an unreadable read is unknown, not "none"
                option_work = {
                    "state": "unknown",
                    "runs": [],
                    "reason": "option_run_read_failed",
                }

            # pending commitments (evidence only; the barrier owns quiescence)
            pending = session.execute(
                select(StrategyReservation.reservation_id).where(
                    StrategyReservation.account_id == account_id,
                    StrategyReservation.strategy_id == strategy_id,
                    StrategyReservation.execution_environment == environment,
                    StrategyReservation.status.in_(("active", "renewed", "action_required")),
                )
            ).all()
        finally:
            session.close()

        # Work state comes from the barrier's OWN in-flight enumeration, which is
        # independent of whether a proof has been RECORDED yet: the proof is
        # recorded after this assessment, so deriving "settled" from proof
        # validity here would deadlock the verdict (nothing would ever be
        # eligible). An unreadable enumeration is unknown, never "settled".
        work_state = "unknown"
        try:
            from backend.strategies.settlement import enumerate_inflight_work

            probe = self._session_factory()
            try:
                inflight = enumerate_inflight_work(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=environment,
                    db=probe,
                )
            finally:
                probe.close()
            work_state = "outstanding" if inflight else "settled"
            if inflight:
                notes.append(
                    "inflight="
                    + ",".join(sorted({f"{item.kind}:{item.ref}" for item in inflight}))
                )
        except Exception:  # noqa: BLE001 - unreadable work evidence is unknown
            unavailable.append("settlement_inflight")
            work_state = "unknown"

        if pending:
            notes.append(f"pending_commitments={len(pending)}")
        if option_work.get("runs"):
            notes.append(
                "option_runs=" + ",".join(str(item) for item in option_work["runs"])
            )
        if option_work.get("state") == "unknown":
            notes.append(
                "this strategy's option runs could not be read completely "
                f"(reason={option_work.get('reason') or 'unknown'})"
            )

        return ContinuationEvidence(
            job_id=str(getattr(job, "id", "")),
            strategy_id=strategy_id,
            owner_id=str(getattr(job, "owner_id", "") or ""),
            account_id=account_id,
            execution_environment=environment,
            attempt=int(getattr(job, "attempt", 1) or 1),
            lease_epoch=int(getattr(job, "lease_epoch", 0) or 0),
            run_id=None if run_id is None else str(run_id),
            job_kind=str(getattr(job, "job_kind", "") or ""),
            job_status=str(getattr(job, "status", "") or ""),
            reconciled=getattr(job, "reconciled_at", None) is not None,
            desired_state=str(getattr(job, "desired_state", "") or "started"),
            barrier_version=barrier_version,
            projection_version=book.get("projection_version"),
            completion_state=completion_state,
            exit_code=getattr(job, "exit_code", None),
            trade_capable=trade_capable,
            process_cleanup_state=getattr(job, "process_cleanup_state", None),
            authority_state=authority_state,
            work_state=work_state,
            exposure_state=exposure_state,
            protection_state=protection_state,
            protection_enabled=protection_enabled,
            recovery_action_required=recovery_action_required,
            quiescence_state=quiescence_state,
            book_state=str(book.get("book_state") or "unknown"),
            divergence_state=divergence_state,
            approval_state=approval_state,
            option_work_state=str(option_work.get("state") or "unknown"),
            option_runs=[str(item) for item in list(option_work.get("runs") or [])],
            unavailable=sorted(set(unavailable)),
            notes=notes,
        )


def continuation_collector_from_job(
    *,
    session_factory: Callable[[], Any],
    barrier: Optional[ExecutionBarrier] = None,
) -> ContinuationCollector:
    """Convenience constructor (kept explicit so tests can inject both)."""
    return ContinuationCollector(session_factory=session_factory, barrier=barrier)


class ContinuationService:
    """The automatic handover path for one strategy's blocked predecessor.

    This is the SINGLE place the host decides "clear this attempt's block without
    an operator", so the supervised-release path, the shared Run now path and the
    scheduler all reach the same verdict. It never approves the successor's
    trades: it only decides that the predecessor is finished and the book it left
    may be re-adopted.

    Every refusal is persisted (``outcome='blocked'``) only when the attempt
    genuinely looked like a normal completion, so an operator can see the named
    reason instead of a silently stuck block; an ineligible shape (an active job,
    a continuous job) writes nothing.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        repository: Any,
        barrier: Optional[ExecutionBarrier] = None,
        collector: Optional[ContinuationCollector] = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository
        self._barrier = barrier or ExecutionBarrier(session_factory=session_factory)
        self._collector = collector or ContinuationCollector(
            session_factory=session_factory, barrier=self._barrier
        )

    # -- helpers -----------------------------------------------------------

    def _record_blocked(self, job: Any, assessment: ContinuationAssessment, actor_id: str) -> Optional[str]:
        try:
            row = self._repository.record_reconciliation(
                job_id=str(job.id),
                strategy_id=str(job.strategy_id),
                owner_id=str(job.owner_id),
                attempt=int(job.attempt),
                run_id=getattr(job, "run_id", None),
                outcome="blocked",
                reason_code=str(assessment.reason_code),
                evidence={
                    "continuation": assessment.to_dict(),
                    "source": "host_automatic_continuation",
                },
                actor_id=str(actor_id),
            )
            return str(getattr(row, "id", "") or "") or None
        except Exception:  # noqa: BLE001 - an audit failure must not change the verdict
            return None

    def _ensure_proof(
        self, evidence: ContinuationEvidence, actor_id: str
    ) -> tuple[Optional[int], Dict[str, Any]]:
        """Record a durable quiescence proof when one is not already current.

        Returns ``(version, detail)``: the barrier version the proof covers, or
        ``None`` plus the named reason when a proof could not be established
        (work outstanding, unreadable barrier). The detail is the platform's own
        refusal, so the operator sees what is still in flight rather than a bare
        "quiescence unverified".
        """
        try:
            state = self._barrier.state(
                account_id=evidence.account_id,
                strategy_id=evidence.strategy_id,
                execution_environment=evidence.execution_environment,
            )
        except Exception as exc:  # noqa: BLE001 - unreadable stays unproven
            return None, {"reason": "barrier_unreadable", "error": type(exc).__name__}
        if bool(state.get("proof_valid")):
            return int(state.get("barrier_version") or 0), {"reason": "already_current"}
        try:
            result = self._barrier.record_proof(
                account_id=evidence.account_id,
                strategy_id=evidence.strategy_id,
                execution_environment=evidence.execution_environment,
                ref=f"continuation:{evidence.job_id}:{int(evidence.attempt)}",
                detail={
                    "job_id": evidence.job_id,
                    "attempt": int(evidence.attempt),
                    "actor_id": actor_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - a failed proof is not a proof
            return None, {"reason": "proof_error", "error": f"{type(exc).__name__}: {exc}"}
        if not result.recorded:
            return None, {
                "reason": str(result.reason),
                "barrier_version": int(result.barrier_version or 0),
                "inflight": [item.as_dict() for item in (result.inflight or [])],
                "unavailable": list(result.unavailable or []),
            }
        try:
            refreshed = self._barrier.state(
                account_id=evidence.account_id,
                strategy_id=evidence.strategy_id,
                execution_environment=evidence.execution_environment,
            )
        except Exception as exc:  # noqa: BLE001
            return None, {"reason": "barrier_unreadable", "error": type(exc).__name__}
        if not bool(refreshed.get("proof_valid")):
            return None, {"reason": "proof_not_current_after_record"}
        return int(refreshed.get("barrier_version") or 0), {"reason": "recorded"}

    # -- the attempt -------------------------------------------------------

    def attempt(
        self,
        *,
        owner_id: str,
        strategy_id: str,
        completion_state: str = COMPLETION_UNKNOWN,
        actor_id: str = "host:automatic_continuation",
    ) -> Dict[str, Any]:
        job = self._repository.get_blocking_job(str(owner_id), str(strategy_id))
        if job is None:
            return {
                "attempted": False,
                "continued": False,
                "reason_code": CONTINUATION_NOT_BLOCKED,
                "assessment": None,
            }
        if str(job.status) != "recovery_required" or job.reconciled_at is not None:
            # An active (or already cleared) attempt is not a continuation target.
            return {
                "attempted": False,
                "continued": False,
                "reason_code": CONTINUATION_NOT_BLOCKED,
                "job_id": str(job.id),
                "assessment": None,
            }

        evidence = self._collector.collect(job, completion_state=completion_state)
        assessment = assess_continuation(evidence)
        audit_id: Optional[str] = None

        if not assessment.allowed:
            # Only a shape that genuinely looked like a finished finite
            # evaluation is audited; an ineligible shape writes nothing.
            if assessment.reason_code not in (
                CONTINUATION_NOT_BLOCKED,
                CONTINUATION_NOT_FINITE,
            ):
                audit_id = self._record_blocked(job, assessment, actor_id)
            return {
                "attempted": True,
                "continued": False,
                "held": bool(assessment.held),
                "case": assessment.case,
                "reason_code": assessment.reason_code,
                "blocking_reasons": list(assessment.blocking_reasons),
                "job_id": str(job.id),
                "audit_id": audit_id,
                "assessment": assessment.to_dict(),
            }

        barrier_version, proof_detail = self._ensure_proof(evidence, actor_id)
        if barrier_version is None:
            notes = list(assessment.notes) + [f"proof_refused:{proof_detail.get('reason')}"]
            proof = dict(assessment.proof)
            proof["proof_attempt"] = proof_detail
            refused = ContinuationAssessment(
                allowed=False,
                case=CASE_CONTINUATION_BLOCKED,
                reason_code=CONTINUATION_QUIESCENCE_UNVERIFIED,
                blocking_reasons=[CONTINUATION_QUIESCENCE_UNVERIFIED],
                held=assessment.held,
                notes=notes,
                proof=proof,
            )
            audit_id = self._record_blocked(job, refused, actor_id)
            return {
                "attempted": True,
                "continued": False,
                "held": bool(refused.held),
                "case": refused.case,
                "reason_code": refused.reason_code,
                "blocking_reasons": list(refused.blocking_reasons),
                "job_id": str(job.id),
                "audit_id": audit_id,
                "assessment": refused.to_dict(),
            }

        # Re-collect immediately before the commit: a change to cleanup,
        # authority, protection, the book or the barrier fails closed instead of
        # clearing the block on stale evidence (the same narrow TOCTOU guard the
        # operator route uses).
        recheck = self._collector.collect(job, completion_state=completion_state)
        reassessed = assess_continuation(recheck)
        if (
            continuation_digest(recheck) != continuation_digest(evidence)
            or not reassessed.allowed
        ):
            changed = ContinuationAssessment(
                allowed=False,
                case=CASE_CONTINUATION_BLOCKED,
                reason_code="EVIDENCE_CHANGED",
                blocking_reasons=list(reassessed.blocking_reasons) or ["EVIDENCE_CHANGED"],
                held=reassessed.held,
                notes=list(reassessed.notes),
                proof=dict(reassessed.proof),
            )
            audit_id = self._record_blocked(job, changed, actor_id)
            return {
                "attempted": True,
                "continued": False,
                "held": bool(changed.held),
                "case": changed.case,
                "reason_code": changed.reason_code,
                "blocking_reasons": list(changed.blocking_reasons),
                "job_id": str(job.id),
                "audit_id": audit_id,
                "assessment": changed.to_dict(),
            }

        evidence_json = recheck.to_dict()
        evidence_json["continuation_proof"] = reassessed.proof
        evidence_json["source"] = "host_automatic_continuation"
        # Protection ownership: an attempt WITH a standing protection policy never
        # reaches here (it is refused by name above), so nothing standing is lost
        # by closing this run. Closing it is required, not optional: a run left
        # open would keep being treated as a protection owner forever.
        close_run = bool(recheck.run_id)
        audit = self._repository.reconcile_with_audit(
            str(job.id),
            owner_id=str(job.owner_id),
            expected_lease_epoch=int(job.lease_epoch),
            expected_attempt=int(job.attempt),
            expected_process_cleanup_state=recheck.process_cleanup_state,
            expected_run_id=recheck.run_id,
            reason_code=CONTINUATION_ELIGIBLE,
            evidence=evidence_json,
            actor_id=actor_id,
            settlement_barrier=self._barrier,
            barrier_account_id=str(recheck.account_id),
            barrier_strategy_id=str(recheck.strategy_id),
            barrier_environment=str(recheck.execution_environment),
            expected_barrier_version=int(barrier_version),
            require_barrier_proof=True,
            expected_projection_version=(
                None if recheck.projection_version is None else int(recheck.projection_version)
            ),
            require_clean_completion=True,
            outcome="continuation",
            close_worker_run=close_run,
            worker_run_id=recheck.run_id,
        )
        if audit is None:
            lost = ContinuationAssessment(
                allowed=False,
                case=CASE_CONTINUATION_BLOCKED,
                reason_code="RECONCILE_RACE_LOST",
                blocking_reasons=["RECONCILE_RACE_LOST"],
                held=reassessed.held,
                notes=list(reassessed.notes),
                proof=dict(reassessed.proof),
            )
            audit_id = self._record_blocked(job, lost, actor_id)
            return {
                "attempted": True,
                "continued": False,
                "held": bool(lost.held),
                "case": lost.case,
                "reason_code": lost.reason_code,
                "blocking_reasons": list(lost.blocking_reasons),
                "job_id": str(job.id),
                "audit_id": audit_id,
                "assessment": lost.to_dict(),
            }

        return {
            "attempted": True,
            "continued": True,
            "held": bool(recheck.held),
            "case": reassessed.case,
            "reason_code": CONTINUATION_ELIGIBLE,
            "blocking_reasons": [],
            "job_id": str(job.id),
            "audit_id": str(getattr(audit, "id", "") or "") or None,
            "barrier_version": int(barrier_version),
            "projection_version": recheck.projection_version,
            "worker_run_closed": close_run,
            "assessment": reassessed.to_dict(),
        }


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)
