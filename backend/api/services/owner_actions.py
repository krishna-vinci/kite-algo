"""Owner actions for one hosted strategy (B2.6b S1): cancel qualifying pending
entry work, and dispose of one provably dead plan submission.

This module is the whole domain layer behind
``backend.api.routers.strategy_owner_actions``; the router only maps HTTP to it.
Two rules shape everything here:

**The server decides, and only from durable evidence.** The owner supplies a
digest, a reason and (for a dead submission) a disposition. Never a quantity,
an account, an environment or an order id to cancel: candidates come from the
strategy's own plan trail (``strategy_plan_execution_events`` plus
``paper_order_fill_progress``) and its own live claims
(``live_plan_submissions`` plus the broker order projection). An order whose
ownership, remaining quantity or entry basis cannot be proved is refused by
name rather than cancelled.

**Nothing risk-reducing or protective is ever touched.** An entry step that
does not increase this strategy's attributed book is refused
(``CANCEL_REDUCTION_FORBIDDEN``); an option hedge, or a long leg that covers a
short of the same option type, is refused (``CANCEL_PROTECTIVE_ORDER_FORBIDDEN``)
unless the frozen ``protection_policy.naked`` declares the structure naked. A
staged protective exit is refused outright: it resolves through
``StagedStructureExit``'s own records, not through a disposition.

The two actions are idempotent. Their action key is
``owner-cancel:{strategy}:{account}:{env}:{plan}:{step}`` /
``dead-submission:{strategy}:{account}:{plan}:{step}``: the broker/paper
cancellation and the barrier event are safe to repeat, while the trail mutation
happens once and reports the row it already wrote.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import select, text

from backend.api.services.hosted_strategy_authz import authorize_account_scope
from backend.options.execution.repair import (
    STATE_FLAT,
    TERMINAL_RUN_STATUSES,
    OptionRunRepairRefusal,
)
from backend.strategies.attribution_models import (
    EXECUTION_ENVIRONMENTS,
    LivePlanSubmission,
    PaperOrderFillProgress,
    StrategyApproval,
    StrategyFlattenOperation,
    StrategyPlan,
    StrategyPlanExecutionEvent,
    StrategyPlanOptionRun,
    StrategyPositionProjection,
    StrategyProposalJournal,
    StrategyRunBinding,
)

#: Coverage verdict. ``unknown`` means the list is NOT complete.
COVERAGE_KNOWN = "known"
COVERAGE_UNKNOWN = "unknown"

#: Per-item eligibility.
ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"

#: The shared response statuses (§5).
STATUS_COMPLETE = "complete"
STATUS_BLOCKED = "blocked"

#: The one cancel disposition the trail records for the cancelled remainder.
DISPOSITION_OWNER_CANCELLED = "owner_cancelled"

#: The dead-submission dispositions §4 permits, in a stable order.
DEAD_SUBMISSION_DISPOSITIONS = (
    "filled",
    "rejected",
    "cancelled",
    "failed_never_submitted",
    "failed_residual_abandoned",
)

#: Named refusals (§5). The cancel three describe WHY a candidate is not
#: cancellable; the digest one is the only one a POST raises for a candidate that
#: WAS cancellable when the owner looked at it.
CANCEL_EVIDENCE_CHANGED = "CANCEL_EVIDENCE_CHANGED"
CANCEL_ORDER_NOT_OWNED = "CANCEL_ORDER_NOT_OWNED"
CANCEL_PROTECTIVE_ORDER_FORBIDDEN = "CANCEL_PROTECTIVE_ORDER_FORBIDDEN"
CANCEL_REDUCTION_FORBIDDEN = "CANCEL_REDUCTION_FORBIDDEN"

DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE = "DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE"
DEAD_SUBMISSION_EVIDENCE_CHANGED = "DEAD_SUBMISSION_EVIDENCE_CHANGED"
DEAD_SUBMISSION_OPEN_REMAINDER = "DEAD_SUBMISSION_OPEN_REMAINDER"
DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN = "DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN"
#: §4 requires refusing a disposition the platform's evidence does not support;
#: §5's list names the evidence-state refusals only, so this one is named after
#: the existing ``OPTION_RUN_REPAIR_ACTION_MISMATCH`` precedent.
DEAD_SUBMISSION_DISPOSITION_MISMATCH = "DEAD_SUBMISSION_DISPOSITION_MISMATCH"
#: §4 requires refusing while an active evaluator could still place work; named
#: after the existing ``FLATTEN_EVALUATION_ACTIVE`` (§5) precedent.
DEAD_SUBMISSION_EVALUATION_ACTIVE = "DEAD_SUBMISSION_EVALUATION_ACTIVE"

# ---- flatten (B2.6b S3, §3) -----------------------------------------------

#: The one pre-action refusal: flatten may not start while a live evaluation
#: authority could still place work, and nothing is moved when it is returned.
FLATTEN_EVALUATION_ACTIVE = "FLATTEN_EVALUATION_ACTIVE"
#: Unanswered work the platform cannot resolve on its own must be dispositioned
#: first (§3 step 2): flatten never guesses whether an order exists.
DEAD_SUBMISSION_UNRESOLVED = "DEAD_SUBMISSION_UNRESOLVED"
#: An item-level refusal: the derived plan would INCREASE exposure, so it is
#: refused BEFORE admission. A flatten plan may only reduce.
FLATTEN_PLAN_INCREASES_EXPOSURE = "FLATTEN_PLAN_INCREASES_EXPOSURE"
#: A reduction plan needs a bound run to attribute its fills to; without one the
#: platform refuses rather than inventing an attribution.
FLATTEN_REDUCTION_RUN_UNBOUND = "FLATTEN_REDUCTION_RUN_UNBOUND"
FLATTEN_REDUCTION_PLAN_REFUSED = "FLATTEN_REDUCTION_PLAN_REFUSED"
FLATTEN_REDUCTION_PIPELINE_UNAVAILABLE = "FLATTEN_REDUCTION_PIPELINE_UNAVAILABLE"
#: A book whose canonical instrument type cannot be read is never flushed: the
#: platform cannot say whether it belongs to an option structure.
FLATTEN_REDUCTION_INSTRUMENT_UNKNOWN = "FLATTEN_REDUCTION_INSTRUMENT_UNKNOWN"
#: Unresolved (``raw``) exposure: the platform cannot say WHICH instrument it is.
FLATTEN_UNATTRIBUTED_EXPOSURE = "FLATTEN_UNATTRIBUTED_EXPOSURE"
#: The option-run set could not be read completely, so no run may be exited on the
#: strength of an incomplete list.
FLATTEN_OPTION_RUN_COVERAGE_UNKNOWN = "FLATTEN_OPTION_RUN_COVERAGE_UNKNOWN"

#: The operation's own status vocabulary. ``complete`` is reserved for "every §3
#: done condition holds"; a resumable operation is ``in_progress`` (waiting on
#: fills) or ``blocked`` (a named refusal stopped an item).
STATUS_IN_PROGRESS = "in_progress"

#: One manifest item kind per §3 step.
FLATTEN_ITEM_CANCEL = "cancel_pending"
FLATTEN_ITEM_OPTION_EXIT = "option_exit"
FLATTEN_ITEM_REDUCTION = "nonoption_reduction"

ITEM_STATE_PENDING = "pending"
ITEM_STATE_DONE = "done"
ITEM_STATE_IN_PROGRESS = "in_progress"
ITEM_STATE_BLOCKED = "blocked"

#: §3's done conditions, one name each. ``complete`` requires ALL of them.
DONE_NO_PENDING_ENTRY = "no_qualifying_pending_entry"
DONE_NO_LIVE_UNRESOLVED = "no_live_unresolved_submission"
DONE_OPTION_RUNS_FLAT = "option_runs_flat"
DONE_BOOKS_ZERO = "books_zero"
DONE_NO_INFLIGHT_WORK = "no_in_flight_governed_work"
DONE_NO_EVALUATION_AUTHORITY = "no_live_evaluation_authority"

#: The hosted job statuses that mean "this evaluation may still place work".
ACTIVE_JOB_STATUSES = ("queued", "starting", "running")
#: The job statuses that are terminal for a stop (the operator stop route's own
#: set: ``cancelled`` is not one of them, and an unknown status is never proof).
TERMINAL_JOB_STATUSES = ("stopped", "failed", "recovery_required")
#: The catalog instrument types that are options. Their books are closed by their
#: OWN run's staged exit, never by a single-instrument reduction plan.
OPTION_INSTRUMENT_TYPES = ("CE", "PE")

#: Per-item outcomes that are NOT named refusals (the response is ``blocked``).
OUTCOME_CANCELLED = "cancelled"
OUTCOME_ALREADY_CANCELLED = "already_cancelled"
OUTCOME_SKIPPED = "skipped"
OUTCOME_BLOCKED = "blocked"
OUTCOME_DISPOSED = "disposed"

REASON_CANCEL_NOT_VERIFIED = "cancel_not_verified"
REASON_LIVE_BOUNDARY_UNAVAILABLE = "live_broker_cancel_boundary_unavailable"

#: The statuses a paper order can no longer leave.
PAPER_TERMINAL_STATUSES = frozenset({"filled", "cancelled", "rejected", "expired"})

#: Live claim states with no outcome yet, so the step is still somebody's work.
LIVE_UNRESOLVED_STATES = frozenset(
    {"pending", "withheld", "releasing", "partial", "finalizing", "rejecting",
     "repair_required", "uncertain"}
)

#: The trail events the plan-execution fold treats as a TERMINAL outcome
#: (``backend.options.execution.plan_binding``). A disposition writes one of
#: these, which is what makes the fold report the plan ``finished`` afterwards.
FOLD_TERMINAL_EVENTS = frozenset({"filled", "rejected", "cancelled", "no_op"})

_LEG_ROLES = ("hedge", "short", "naked")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OwnerActionRefusal(RuntimeError):
    """A named owner-action refusal, mapped by the router to a 409 + detail."""

    def __init__(
        self,
        reason_code: str,
        detail: Optional[Mapping[str, Any]] = None,
        *,
        status_code: int = 409,
    ) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        self.status_code = int(status_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def _environment_for(hosted: Any, requested: Optional[str]) -> str:
    """The environment for this action, derived from the strategy itself.

    An explicit ``environment`` is validated and honoured; omitted, the
    strategy's own default execution mode decides. Both speak the same
    vocabulary, so the action can never be pointed at a book the strategy does
    not act in.
    """
    wanted = str(requested or "").strip().lower()
    if wanted:
        if wanted not in EXECUTION_ENVIRONMENTS:
            raise HTTPException(
                status_code=422,
                detail=f"environment must be one of {', '.join(EXECUTION_ENVIRONMENTS)}",
            )
        return wanted
    mode = str(getattr(hosted, "default_execution_mode", "") or "").strip().lower()
    return mode if mode in EXECUTION_ENVIRONMENTS else "live"


def owner_action_scope(
    repo: Any,
    owner: str,
    strategy_id: str,
    requested_environment: Optional[str] = None,
) -> Dict[str, Any]:
    """The strategy's own ``(account, environment)``, never a caller's claim.

    Foreign and missing are indistinguishable on purpose (404), and the account
    is authorized against the configured hosted allowlist before anything is
    read - exactly the scoping the governed option-run repair path applies.
    """
    hosted = repo.get_strategy(owner, strategy_id)
    if hosted is None:
        # Foreign and missing are indistinguishable on purpose.
        raise HTTPException(status_code=404, detail="Strategy not found")
    canonical = repo.get_canonical_strategy(owner, strategy_id) or hosted
    account_id = str(
        getattr(canonical, "account_scope", "")
        or getattr(hosted, "default_account_scope", "")
    ).strip()
    if not account_id:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(account_id)
    return {
        "strategy_id": str(strategy_id),
        "account_id": account_id,
        "execution_environment": _environment_for(hosted, requested_environment),
        "hosted": hosted,
        #: The hosted owner the job ledger is keyed by (``app:<username>``). Never
        #: a caller value: it comes from ``require_strategy_owner``.
        "owner_id": str(owner),
    }


# ---------------------------------------------------------------------------
# frozen-plan readers
# ---------------------------------------------------------------------------


def _frozen_leg(resolved: Mapping[str, Any], step_no: int) -> Optional[Dict[str, Any]]:
    """The frozen leg a 1-based step number names, or ``None`` when unreadable.

    The executor derives one step per frozen leg in list order
    (``PaperPlanExecutor._plan_steps``), so the step number IS the leg index.
    """
    legs = (resolved or {}).get("legs")
    if not isinstance(legs, list):
        return None
    index = int(step_no) - 1
    if index < 0 or index >= len(legs):
        return None
    leg = legs[index]
    return dict(leg) if isinstance(leg, Mapping) else None


def _frozen_leg_quantity(leg: Mapping[str, Any]) -> Optional[int]:
    """One frozen leg's unsigned magnitude, or ``None`` when unreadable."""
    for key in ("quantity", "signed_quantity"):
        value = _as_int(leg.get(key))
        if value is not None:
            return abs(value)
    return None


def _covering_long(resolved: Mapping[str, Any], leg: Mapping[str, Any]) -> Optional[bool]:
    """Whether a LONG leg covers a short of the same option type.

    ``None`` means the coverage basis is unreadable (no option type on the leg),
    which refuses rather than becoming cancellable. The dimension is the option
    type, exactly as the compiler's structure and
    ``option_adjust_would_unhedge`` treat it: the structure carries one
    underlying and one expiry, so a short can only be covered within its type.
    """
    option_type = str(leg.get("option_type") or "").strip().upper()
    if not option_type:
        return None
    legs = (resolved or {}).get("legs")
    if not isinstance(legs, list):
        return None
    for other in legs:
        if not isinstance(other, Mapping) or other is leg:
            continue
        if str(other.get("side") or "").strip().upper() != "SELL":
            continue
        if str(other.get("option_type") or "").strip().upper() == option_type:
            return True
    return False


# ---------------------------------------------------------------------------
# policy checks
# ---------------------------------------------------------------------------


class _CancelBasis:
    """Why one candidate is (or is not) cancellable entry work."""

    def __init__(self, plan_kind: str, resolved: Mapping[str, Any], leg: Mapping[str, Any]) -> None:
        self.plan_kind = str(plan_kind or "")
        self.resolved = dict(resolved or {})
        self.leg = dict(leg or {})

    def classify(self, attributed_open: Optional[int]) -> Tuple[str, Optional[str]]:
        if self.plan_kind == "option_structure":
            return self._classify_option()
        return self._classify_generic(attributed_open)

    def _classify_option(self) -> Tuple[str, Optional[str]]:
        """§1's option rule: role first, then coverage, then the naked declaration.

        ``role == "hedge"`` is protective. A long leg that covers a short of the
        same option type is protective, unless the frozen policy declares the
        structure naked. A role that cannot be read refuses: an unreadable basis
        is never a licence to cancel what might be protection.
        """
        policy = self.resolved.get("protection_policy")
        naked = bool(policy.get("naked")) if isinstance(policy, Mapping) else False
        role = str(self.leg.get("role") or "").strip().lower()
        if role not in _LEG_ROLES:
            return INELIGIBLE, CANCEL_ORDER_NOT_OWNED
        if _frozen_leg_quantity(self.leg) is None:
            return INELIGIBLE, CANCEL_ORDER_NOT_OWNED
        if role == "hedge":
            return INELIGIBLE, CANCEL_PROTECTIVE_ORDER_FORBIDDEN
        side = str(self.leg.get("side") or "").strip().upper()
        if side == "BUY":
            covers = _covering_long(self.resolved, self.leg)
            if covers is None:
                return INELIGIBLE, CANCEL_ORDER_NOT_OWNED
            if covers and not naked:
                return INELIGIBLE, CANCEL_PROTECTIVE_ORDER_FORBIDDEN
        return ELIGIBLE, None

    def _classify_generic(self, attributed_open: Optional[int]) -> Tuple[str, Optional[str]]:
        """Non-option entry: the signed delta decides, from attributed evidence.

        The target is the frozen signed quantity (the instruction). A plan whose
        size is derived at execution time (a ``target_weights`` leg) carries no
        target the trail can prove, so its basis is unreadable here and it is
        refused rather than guessed at.
        """
        target = _as_int(self.leg.get("signed_quantity"))
        if target is None:
            return INELIGIBLE, CANCEL_ORDER_NOT_OWNED
        if attributed_open is None:
            return INELIGIBLE, CANCEL_ORDER_NOT_OWNED
        if not _opens_or_grows_exposure(target, attributed_open):
            return INELIGIBLE, CANCEL_REDUCTION_FORBIDDEN
        return ELIGIBLE, None


def _opens_or_grows_exposure(target: int, current: int) -> bool:
    """The executor's own admission classifier, reused so the two cannot disagree.

    ``PaperPlanExecutor._opens_or_grows_exposure`` (D-6): the book grows, or the
    trade crosses flat. Everything else is a reduction or a close, which this
    action never touches.
    """
    from backend.strategies.execution import PaperPlanExecutor

    return bool(PaperPlanExecutor._opens_or_grows_exposure(int(target), int(current)))


# ---------------------------------------------------------------------------
# platform readers
# ---------------------------------------------------------------------------


def _paper_order_row(session: Any, account_id: str, paper_order_id: str) -> Optional[Dict[str, Any]]:
    """One paper order's status/fill evidence, or ``None`` when unreadable."""
    if not paper_order_id:
        return None
    try:
        row = session.execute(
            text(
                "SELECT status, quantity, filled_quantity, pending_quantity "
                "FROM public.paper_orders WHERE account_scope = :account "
                "AND order_id = :order_id"
            ),
            {"account": str(account_id), "order_id": str(paper_order_id)},
        ).mappings().first()
    except Exception:  # noqa: BLE001 - an unreadable order is never "absent"
        return None
    return dict(row) if row is not None else None


def _paper_progress_row(session: Any, account_id: str, paper_order_id: str) -> Optional[Dict[str, Any]]:
    """The paper fill-progress row, which is the authoritative tranche state."""
    if not paper_order_id:
        return None
    try:
        row = session.execute(
            select(PaperOrderFillProgress).where(
                PaperOrderFillProgress.account_scope == str(account_id),
                PaperOrderFillProgress.paper_order_id == str(paper_order_id),
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return {
        "filled_quantity": int(row.filled_quantity or 0),
        "remaining_quantity": int(row.remaining_quantity or 0),
        "status": str(row.status or ""),
    }


def _paper_order_for_step(
    session: Any, account_id: str, plan_id: str, step_no: int
) -> Optional[str]:
    """A paper order this plan/step is attributed to, or ``None`` if there is none.

    This is the paper lane's pre-send fence: the executor stamps every paper order
    with the plan and step that produced it, so a step the runtime reached has an
    order even when the response never came back. ``None`` therefore means the
    step never reached the order path; an UNREADABLE search is reported
    separately and never promoted to "no order".
    """
    dialect = str(getattr(session.bind, "dialect", None).name if session.bind is not None else "")
    params = {
        "account": str(account_id),
        "plan": str(plan_id),
        "step": str(int(step_no)),
    }
    if dialect == "postgresql":
        sql = (
            "SELECT order_id FROM public.paper_orders WHERE account_scope = :account "
            "AND metadata_json->>'plan_id' = :plan "
            "AND metadata_json->>'step_no' = :step "
            "ORDER BY placed_at DESC LIMIT 1"
        )
    else:
        sql = (
            "SELECT order_id FROM public.paper_orders WHERE account_scope = :account "
            "AND json_extract(metadata_json, '$.plan_id') = :plan "
            "AND CAST(json_extract(metadata_json, '$.step_no') AS TEXT) = :step "
            "ORDER BY placed_at DESC LIMIT 1"
        )
    row = session.execute(text(sql), params).first()
    return None if row is None else str(row[0])


def _broker_projection(session: Any, account_id: str, order_ids: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The broker order projection's terminal/fill verdict for known order ids."""
    wanted = [str(order_id) for order_id in order_ids if str(order_id or "")]
    if not wanted:
        return {"terminal": False, "status": "", "filled_quantity": 0, "known": []}
    placeholders = ", ".join(f":order{index}" for index in range(len(wanted)))
    params: Dict[str, Any] = {"account": str(account_id)}
    params.update({f"order{index}": value for index, value in enumerate(wanted)})
    try:
        rows = session.execute(
            text(
                "SELECT order_id, latest_status, terminal, last_seen_filled_quantity "
                f"FROM public.order_state_projection WHERE account_id = :account "
                f"AND order_id IN ({placeholders})"
            ),
            params,
        ).mappings().all()
    except Exception:  # noqa: BLE001 - an unreadable projection is never terminal
        return None
    found = {str(row["order_id"]): dict(row) for row in rows}
    if any(order_id not in found for order_id in wanted):
        # An order the projection cannot see is unknown, not cancelled.
        return {"terminal": False, "status": "", "filled_quantity": 0, "known": sorted(found)}
    terminal = all(bool(row["terminal"]) for row in found.values())
    return {
        "terminal": terminal,
        "status": str(next(iter(found.values()))["latest_status"] or "") if found else "",
        "filled_quantity": sum(int(row["last_seen_filled_quantity"] or 0) for row in found.values()),
        "known": sorted(found),
    }


def _attributed_open(
    session: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
    leg: Mapping[str, Any],
) -> Optional[int]:
    """This strategy's signed attributed quantity for one leg's instrument.

    ``None`` means the basis could not be read, which refuses rather than
    defaulting to flat (a flat default would classify every step as an entry).
    """
    instrument_id = str(leg.get("instrument_id") or "")
    product = str(leg.get("product") or "")
    if not instrument_id:
        return None
    try:
        rows = session.execute(
            select(StrategyPositionProjection.net_quantity).where(
                StrategyPositionProjection.account_id == str(account_id),
                StrategyPositionProjection.strategy_id == str(strategy_id),
                StrategyPositionProjection.execution_environment == str(execution_environment),
                StrategyPositionProjection.identity_kind == "canonical",
                StrategyPositionProjection.canonical_instrument_id == instrument_id,
                StrategyPositionProjection.product == product,
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        return None
    return int(sum(int(value or 0) for value in rows))


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class OwnerActionsService:
    """Preview, cancel and dispose - always from the strategy's own evidence."""

    def __init__(
        self,
        *,
        session_factory: Any,
        run_store: Any = None,
        paper_service: Any = None,
        barrier: Any = None,
        repository: Any = None,
        broker_cancel: Any = None,
        flatten_store: Any = None,
        snapshot_service: Any = None,
        option_exit_runner: Any = None,
        reduction_plan_builder: Any = None,
        reduction_pipeline: Any = None,
    ) -> None:
        self.session_factory = session_factory
        self.run_store = run_store
        self.paper_service = paper_service
        self.repository = repository
        #: The durable flatten operations (B2.6b S3). Absent, the default store
        #: over this session factory is used.
        self.flatten_store = flatten_store
        #: The scope-derived option-run / pending-work snapshot reads. Absent, the
        #: platform's own ``OwnedWorkSnapshotService`` is used.
        self.snapshot_service = snapshot_service
        #: ``async (scope, option_run_id, *, reason) -> dict``: the S2 owner exit
        #: for ONE run. Absent, an option run cannot be exited and is reported
        #: blocked rather than assumed flat.
        self.option_exit_runner = option_exit_runner
        #: ``(scope, book, *, operation_id, actor) -> {"plan": ..., "plan_id": ...}``
        #: - the frozen target-zero plan for ONE attributed ``(instrument, product)``
        #: book. Absent, ``default_reduction_planner`` builds it through the
        #: proposal/compile path.
        self.reduction_plan_builder = (
            reduction_plan_builder
            if reduction_plan_builder is not None
            else default_reduction_planner(session_factory)
        )
        #: The governed execute route's paper pipeline: ``.admit(plan, environment=)``
        #: and ``async .execute(plan, actor=)``. Absent, a reduction is reported
        #: blocked by name rather than sent through an unknown boundary.
        self.reduction_pipeline = reduction_pipeline
        #: The existing fake-testable broker cancel boundary, called as
        #: ``broker_cancel(account_id=..., order_id=...)``. Absent (or failing)
        #: means the live cancel is UNKNOWN, never an assumed cancellation.
        self.broker_cancel = broker_cancel
        if barrier is None:
            from backend.strategies.settlement import ExecutionBarrier

            barrier = ExecutionBarrier(session_factory=session_factory)
        self.barrier = barrier

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _lock_plan(session: Any, plan_id: str) -> None:
        """Serialize one plan's trail mutation. PostgreSQL only; SQLite no-op.

        The SAME lock the executor takes before it commits a step
        (``plan-exec:{plan_id}``): a plan's execution state is written by one
        writer at a time, so two concurrent owner actions on the same step
        cannot both append a terminal outcome.
        """
        bind = getattr(session, "bind", None)
        if bind is None or bind.dialect.name != "postgresql":
            return
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"plan-exec:{str(plan_id)}"},
        )

    @staticmethod
    def _disposed_already(session: Any, *, plan_id: str, step_no: int, key: str) -> bool:
        """Whether one owner action already wrote ITS terminal row for this step."""
        rows = session.execute(
            select(StrategyPlanExecutionEvent.detail).where(
                StrategyPlanExecutionEvent.plan_id == str(plan_id),
                StrategyPlanExecutionEvent.step_no == int(step_no),
            )
        ).scalars().all()
        for detail in rows:
            payload = dict(detail or {})
            if str(payload.get("owner_action_key") or "") == str(key):
                return True
        return False

    def _run_store(self) -> Any:
        if self.run_store is None:
            from backend.options.execution.durable_store import DurableOptionRunStore

            self.run_store = DurableOptionRunStore(session_factory=self.session_factory)
        return self.run_store

    def _worker_run_id(self, option_run_id: Optional[str]) -> Optional[str]:
        """The hosted worker run behind one option run, for the job audit.

        The job audit is keyed by the WORKER run, not the option run: the option
        run is the structure, the worker run is the hosted attempt. An unreadable
        or unbound run simply has no job audit - never an invented one.
        """
        if not option_run_id:
            return None
        try:
            run = self._run_store().get_run(str(option_run_id))
        except Exception:  # noqa: BLE001
            return None
        metadata = dict(getattr(run, "metadata", None) or {})
        return str(metadata.get("worker_run_id") or "") or None

    def _plan_rows(self, scope: Mapping[str, Any]) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    StrategyPlan.plan_id,
                    StrategyPlan.plan_kind,
                    StrategyPlan.resolved_plan,
                    StrategyPlan.plan_hash,
                ).where(
                    StrategyPlan.strategy_id == str(scope["strategy_id"]),
                    StrategyPlan.account_id == str(scope["account_id"]),
                )
            ).all()
        return [
            {
                "plan_id": str(row[0]),
                "plan_kind": str(row[1] or ""),
                "resolved_plan": dict(row[2] or {}),
            }
            for row in rows
        ]

    @staticmethod
    def _trail_steps(session: Any, plan_id: str) -> Dict[int, Dict[str, Any]]:
        """Per-step trail evidence: the newest event and the linked order ids."""
        rows = session.execute(
            select(
                StrategyPlanExecutionEvent.step_no,
                StrategyPlanExecutionEvent.event,
                StrategyPlanExecutionEvent.paper_order_id,
                StrategyPlanExecutionEvent.broker_order_id,
                StrategyPlanExecutionEvent.filled_quantity,
                StrategyPlanExecutionEvent.detail,
                StrategyPlanExecutionEvent.created_at,
            )
            .where(StrategyPlanExecutionEvent.plan_id == str(plan_id))
            .order_by(StrategyPlanExecutionEvent.created_at, StrategyPlanExecutionEvent.id)
        ).all()
        steps: Dict[int, Dict[str, Any]] = {}
        for step_no, event, paper_order_id, broker_order_id, filled, detail, created_at in rows:
            entry = steps.setdefault(
                int(step_no or 0),
                {
                    "events": [],
                    "paper_order_id": None,
                    "broker_order_id": None,
                    "filled_quantity": 0,
                    "trail_state": "",
                    "submitted": False,
                    "detail": {},
                    "last_at": None,
                },
            )
            entry["events"].append(str(event or ""))
            entry["trail_state"] = str(event or "")
            entry["submitted"] = entry["submitted"] or str(event or "") == "submitted"
            if paper_order_id:
                entry["paper_order_id"] = str(paper_order_id)
            if broker_order_id:
                entry["broker_order_id"] = str(broker_order_id)
            if filled is not None:
                entry["filled_quantity"] = int(filled)
            if detail:
                entry["detail"] = dict(detail)
            entry["last_at"] = created_at
        return steps

    @staticmethod
    def _phase_for(session: Any, plan_id: str) -> Optional[str]:
        rows = session.execute(
            select(StrategyPlanOptionRun.phase, StrategyPlanOptionRun.option_run_id).where(
                StrategyPlanOptionRun.plan_id == str(plan_id)
            )
        ).all()
        if not rows:
            return None
        for phase, _run_id in rows:
            if str(phase) == "entry":
                return "entry"
        return str(rows[0][0] or "")

    @staticmethod
    def _run_id_for(session: Any, plan_id: str) -> Optional[str]:
        row = session.execute(
            select(StrategyPlanOptionRun.option_run_id).where(
                StrategyPlanOptionRun.plan_id == str(plan_id),
                StrategyPlanOptionRun.phase == "entry",
            )
        ).first()
        return None if row is None else str(row[0])

    @staticmethod
    def _plan_execution_state(session: Any, plan_id: str) -> Dict[str, Any]:
        from backend.options.execution.plan_binding import option_plan_execution_state

        try:
            return dict(option_plan_execution_state(str(plan_id), session=session))
        except Exception:  # noqa: BLE001 - an unreadable fold is never "finished"
            return {"state": "unknown", "evidence": {"reason": "fold_read_failed"}}

    # ------------------------------------------------------------- preview

    def preview_pending(self, scope: Mapping[str, Any]) -> Dict[str, Any]:
        """The stable preview: qualifying candidates, their digest, and coverage."""
        coverage = COVERAGE_KNOWN
        candidates: List[Dict[str, Any]] = []
        plans = self._plan_rows(scope)
        plans_by_id = {plan["plan_id"]: plan for plan in plans}
        with self.session_factory() as session:
            live_rows = self._live_claim_rows(session, scope)
            # Paper candidates only when the action's own environment is paper: a
            # live book never gets a paper order cancelled through this route.
            paper_scope = str(scope["execution_environment"]) == "paper"
            for plan in (plans if paper_scope else []):
                fold = self._plan_execution_state(session, plan["plan_id"])
                steps = self._trail_steps(session, plan["plan_id"])
                if not steps:
                    continue
                if str(fold.get("state") or "") == "unknown":
                    # The plan's own work cannot be folded, so this read is not
                    # complete - but an individual step can still be proven.
                    coverage = COVERAGE_UNKNOWN
                unresolved = [
                    int(step_no)
                    for step_no, entry in steps.items()
                    if entry["submitted"]
                    and not any(event in FOLD_TERMINAL_EVENTS for event in entry["events"])
                ]
                for step_no in sorted(unresolved):
                    entry = steps[step_no]
                    candidate = self._paper_candidate(
                        session, scope, plan=plan, step_no=step_no, entry=entry
                    )
                    if candidate is not None:
                        candidates.append(candidate)
            if live_rows is None:
                coverage = COVERAGE_UNKNOWN
            else:
                for row in live_rows:
                    candidate = self._live_candidate(
                        session, scope, row=row, plans_by_id=plans_by_id
                    )
                    if candidate is not None:
                        candidates.append(candidate)
        candidates.sort(key=lambda item: (str(item.get("plan_id") or ""), int(item.get("step_no") or 0)))
        return {
            "coverage": coverage,
            "evidence_digest": self._preview_digest(scope, candidates),
            "items": candidates,
        }

    @staticmethod
    def _preview_digest(scope: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> str:
        return _digest(
            {
                "strategy_id": str(scope["strategy_id"]),
                "account_id": str(scope["account_id"]),
                "execution_environment": str(scope["execution_environment"]),
                "candidates": [
                    {
                        "plan_id": str(item.get("plan_id") or ""),
                        "step_no": int(item.get("step_no") or 0),
                        "order_id": item.get("order_id"),
                        "remaining_quantity": int(item.get("remaining_quantity") or 0),
                        "eligibility": str(item.get("eligibility") or ""),
                        "reason_code": item.get("reason_code"),
                        "environment": str(item.get("environment") or ""),
                    }
                    for item in candidates
                ],
            }
        )

    def _paper_candidate(
        self,
        session: Any,
        scope: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        step_no: int,
        entry: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        plan_id = str(plan["plan_id"])
        order_id = entry.get("paper_order_id")
        leg = _frozen_leg(plan["resolved_plan"], step_no)
        phase = self._phase_for(session, plan_id)
        if phase is not None and phase != "entry":
            # Exit / adjust / roll work is never a cancellable entry.
            return {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "order_id": order_id,
                "remaining_quantity": 0,
                "eligibility": INELIGIBLE,
                "reason_code": CANCEL_REDUCTION_FORBIDDEN,
                "environment": "paper",
            }
        if leg is None or not order_id:
            # No frozen leg, or no order the platform can point at: the ownership
            # and the remainder are unprovable.
            return {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "order_id": order_id,
                "remaining_quantity": 0,
                "eligibility": INELIGIBLE,
                "reason_code": CANCEL_ORDER_NOT_OWNED,
                "environment": "paper",
            }
        order = _paper_order_row(session, str(scope["account_id"]), str(order_id))
        if order is None:
            return {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "order_id": str(order_id),
                "remaining_quantity": 0,
                "eligibility": INELIGIBLE,
                "reason_code": CANCEL_ORDER_NOT_OWNED,
                "environment": "paper",
            }
        progress = _paper_progress_row(session, str(scope["account_id"]), str(order_id))
        status = str(progress["status"] if progress else order.get("status") or "")
        remaining = _as_int(
            progress["remaining_quantity"] if progress else order.get("pending_quantity")
        )
        if remaining is None:
            remaining = max(
                int(order.get("quantity") or 0) - int(order.get("filled_quantity") or 0), 0
            )
        attributed = None
        if str(plan["plan_kind"]) != "option_structure":
            attributed = _attributed_open(
                session,
                account_id=str(scope["account_id"]),
                strategy_id=str(scope["strategy_id"]),
                execution_environment="paper",
                leg=leg,
            )
        eligibility, reason_code = _CancelBasis(
            plan["plan_kind"], plan["resolved_plan"], leg
        ).classify(attributed)
        if status in PAPER_TERMINAL_STATUSES:
            # The order already stopped; there is nothing to cancel.
            eligibility, reason_code = INELIGIBLE, CANCEL_ORDER_NOT_OWNED
        if remaining <= 0:
            eligibility, reason_code = INELIGIBLE, CANCEL_ORDER_NOT_OWNED
        return {
            "plan_id": plan_id,
            "step_no": int(step_no),
            "order_id": str(order_id),
            "remaining_quantity": int(remaining),
            "eligibility": eligibility,
            "reason_code": reason_code,
            "environment": "paper",
        }

    def _live_claim_rows(self, session: Any, scope: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if str(scope["execution_environment"]) != "live":
            return []
        try:
            rows = session.execute(
                select(
                    LivePlanSubmission.plan_id,
                    LivePlanSubmission.step_no,
                    LivePlanSubmission.state,
                    LivePlanSubmission.broker_order_ids,
                    LivePlanSubmission.delta_snapshot,
                ).where(
                    LivePlanSubmission.strategy_id == str(scope["strategy_id"]),
                    LivePlanSubmission.account_id == str(scope["account_id"]),
                    LivePlanSubmission.execution_environment == "live",
                )
            ).all()
        except Exception:  # noqa: BLE001
            return None
        return [
            {
                "plan_id": str(row[0]),
                "step_no": int(row[1] or 0),
                "state": str(row[2] or ""),
                "broker_order_ids": [str(value) for value in (row[3] or []) if str(value or "")],
                "delta_snapshot": dict(row[4] or {}),
            }
            for row in rows
            if str(row[2] or "") in LIVE_UNRESOLVED_STATES
        ]

    def _live_candidate(
        self,
        session: Any,
        scope: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
        plans_by_id: Mapping[str, Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """One live claim that is still unresolved, with its broker projection."""
        plan_id = str(row["plan_id"])
        step_no = int(row["step_no"])
        order_ids = list(row["broker_order_ids"])
        snapshot = dict(row["delta_snapshot"] or {})
        remaining = _as_int(snapshot.get("remaining_quantity"))
        if remaining is None:
            requested = _as_int(snapshot.get("quantity"))
            filled = _as_int(snapshot.get("filled_quantity")) or 0
            remaining = None if requested is None else max(requested - filled, 0)
        plan = plans_by_id.get(plan_id)
        eligibility = INELIGIBLE
        reason_code: Optional[str] = CANCEL_ORDER_NOT_OWNED
        if plan is not None and remaining is not None and remaining > 0 and order_ids:
            leg = _frozen_leg(plan["resolved_plan"], step_no)
            projection = _broker_projection(session, str(scope["account_id"]), order_ids)
            if leg is not None and projection is not None:
                if not projection["terminal"]:
                    eligibility, reason_code = _CancelBasis(
                        plan["plan_kind"], plan["resolved_plan"], leg
                    ).classify(
                        _attributed_open(
                            session,
                            account_id=str(scope["account_id"]),
                            strategy_id=str(scope["strategy_id"]),
                            execution_environment="live",
                            leg=leg,
                        )
                        if str(plan["plan_kind"]) != "option_structure"
                        else None
                    )
                else:
                    reason_code = CANCEL_ORDER_NOT_OWNED
        return {
            "plan_id": plan_id,
            "step_no": step_no,
            "order_id": order_ids[0] if order_ids else None,
            "order_ids": order_ids,
            "remaining_quantity": int(remaining or 0),
            "eligibility": eligibility,
            "reason_code": reason_code,
            "environment": "live",
        }

    # -------------------------------------------------------------- cancel

    async def cancel_pending(
        self,
        scope: Mapping[str, Any],
        *,
        evidence_digest: str,
        reason: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Cancel qualifying pending entry work, or refuse the whole action.

        The digest is re-derived first: between the owner's look and this call a
        fill, a terminal outcome or a protective stage could have moved the
        evidence, and acting on what they saw would act on what is no longer
        true (``CANCEL_EVIDENCE_CHANGED``).
        """
        preview = self.preview_pending(scope)
        if str(evidence_digest or "") != str(preview["evidence_digest"]):
            raise OwnerActionRefusal(
                CANCEL_EVIDENCE_CHANGED,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "message": (
                        "the strategy's pending entry work changed since it was "
                        "inspected; re-inspect before cancelling"
                    ),
                },
            )
        items: List[Dict[str, Any]] = []
        run_ids: List[str] = []
        for candidate in preview["items"]:
            if str(candidate["eligibility"]) != ELIGIBLE:
                items.append(
                    {
                        "plan_id": str(candidate["plan_id"]),
                        "step_no": int(candidate["step_no"]),
                        "order_id": candidate.get("order_id"),
                        "eligibility": INELIGIBLE,
                        "outcome": OUTCOME_SKIPPED,
                        "remaining_quantity": int(candidate.get("remaining_quantity") or 0),
                        "reason_code": candidate.get("reason_code"),
                    }
                )
                continue
            item, run_id = await self._cancel_candidate(
                scope, candidate=candidate, reason=reason, actor=actor
            )
            items.append(item)
            if run_id:
                run_ids.append(run_id)
        blocked = [item for item in items if item["outcome"] == OUTCOME_BLOCKED]
        refusal = None
        if blocked:
            refusal = str(blocked[0].get("reason_code") or "")
        option_run_id = run_ids[0] if run_ids else None
        audit_id = self.record_audit(
            scope,
            action="cancel_pending",
            actor=actor,
            evidence={"evidence_digest": str(preview["evidence_digest"]), "items": items},
            run_id=self._worker_run_id(option_run_id),
            option_run_id=option_run_id,
        )
        return {
            "status": STATUS_BLOCKED if blocked else STATUS_COMPLETE,
            "action_id": str(uuid.uuid4()),
            "evidence_digest": str(preview["evidence_digest"]),
            "items": items,
            "refusal": refusal,
            "audit_id": audit_id,
        }

    async def _cancel_candidate(
        self,
        scope: Mapping[str, Any],
        *,
        candidate: Mapping[str, Any],
        reason: str,
        actor: str,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        plan_id = str(candidate["plan_id"])
        step_no = int(candidate["step_no"])
        order_id = str(candidate.get("order_id") or "")
        key = self._action_key(scope, plan_id, step_no)
        environment = str(candidate.get("environment") or "paper")
        already = self._cancel_already_applied(plan_id, step_no, key=key)
        if already is not None:
            return (
                {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "order_id": order_id or None,
                    "outcome": OUTCOME_ALREADY_CANCELLED,
                    "filled_quantity": int(already.get("filled_quantity") or 0),
                    "remaining_quantity": 0,
                    "disposition": DISPOSITION_OWNER_CANCELLED,
                    "run_status": None,
                    "reason_code": None,
                },
                None,
            )
        if environment == "live":
            verified, filled, _platform_status = await self._cancel_live(
                scope, order_ids=list(candidate.get("order_ids") or []), order_id=order_id
            )
        else:
            verified, filled, _platform_status = await self._cancel_paper(
                scope, order_id=order_id
            )
        if not verified:
            # An ambiguous cancel is NEVER an assumed cancellation: the order may
            # still be working, so this becomes ordinary dead-submission evidence
            # work rather than a trail row that claims it stopped.
            return (
                {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "order_id": order_id or None,
                    "outcome": OUTCOME_BLOCKED,
                    "filled_quantity": int(filled or 0),
                    "remaining_quantity": int(candidate.get("remaining_quantity") or 0),
                    "disposition": None,
                    "run_status": None,
                    "reason_code": REASON_CANCEL_NOT_VERIFIED
                    if environment == "paper"
                    else REASON_LIVE_BOUNDARY_UNAVAILABLE,
                },
                None,
            )
        run_status, run_id, created = self._settle_cancelled_step(
            scope,
            plan_id=plan_id,
            step_no=step_no,
            order_id=order_id,
            filled=int(filled or 0),
            reason=reason,
            actor=actor,
            key=key,
            environment=environment,
        )
        if not created:
            # The plan already carries this action's outcome: the cancellation
            # is idempotent, but the trail is never written twice.
            return (
                {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "order_id": order_id or None,
                    "outcome": OUTCOME_ALREADY_CANCELLED,
                    "filled_quantity": int(filled or 0),
                    "remaining_quantity": 0,
                    "disposition": DISPOSITION_OWNER_CANCELLED,
                    "run_status": None,
                    "reason_code": None,
                },
                None,
            )
        return (
            {
                "plan_id": plan_id,
                "step_no": step_no,
                "order_id": order_id or None,
                "outcome": OUTCOME_CANCELLED,
                "filled_quantity": int(filled or 0),
                "remaining_quantity": 0,
                "disposition": DISPOSITION_OWNER_CANCELLED,
                "run_status": run_status,
                "reason_code": None,
            },
            run_id,
        )

    @staticmethod
    def _action_key(scope: Mapping[str, Any], plan_id: str, step_no: int) -> str:
        """§1's action key: the whole scope plus the one step it settles."""
        return "owner-cancel:{}:{}:{}:{}:{}".format(
            str(scope["strategy_id"]),
            str(scope["account_id"]),
            str(scope["execution_environment"]),
            str(plan_id),
            int(step_no),
        )

    def _cancel_already_applied(self, plan_id: str, step_no: int, *, key: str) -> Optional[Dict[str, Any]]:
        """This action key's own trail rows, or ``None`` when it was never applied.

        The proven fill lives on the ``partially_filled`` row and the disposition
        on the ``failed`` row, so both are summed: reporting the cancelled
        remainder's own (zero) quantity as the fill would understate what the
        book kept.
        """
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    StrategyPlanExecutionEvent.event,
                    StrategyPlanExecutionEvent.filled_quantity,
                    StrategyPlanExecutionEvent.detail,
                )
                .where(
                    StrategyPlanExecutionEvent.plan_id == str(plan_id),
                    StrategyPlanExecutionEvent.step_no == int(step_no),
                )
                .order_by(StrategyPlanExecutionEvent.created_at, StrategyPlanExecutionEvent.id)
            ).all()
        captured = 0
        disposition_seen = False
        for _event, filled, detail in rows:
            payload = dict(detail or {})
            if str(payload.get("owner_action_key") or "") != str(key):
                continue
            captured += int(filled or 0)
            if str(payload.get("disposition") or "") == DISPOSITION_OWNER_CANCELLED:
                disposition_seen = True
        if not disposition_seen:
            return None
        return {"filled_quantity": captured}

    async def _cancel_paper(
        self, scope: Mapping[str, Any], *, order_id: str
    ) -> Tuple[bool, int, str]:
        """Cancel on paper through the runtime boundary, then VERIFY terminal."""
        if self.paper_service is None:
            return False, 0, ""
        try:
            await _await_maybe(
                self.paper_service.cancel_order(
                    account_scope=str(scope["account_id"]), paper_order_id=str(order_id)
                )
            )
        except Exception:  # noqa: BLE001 - an unverifiable cancel is not a cancel
            return False, 0, ""
        filled, remaining, status = self._paper_terminal_state(scope, order_id=order_id)
        verified = status == "cancelled" and remaining == 0
        return verified, filled, status

    def _paper_terminal_state(
        self, scope: Mapping[str, Any], *, order_id: str
    ) -> Tuple[int, Optional[int], str]:
        """``(filled, remaining, status)`` from the durable paper rows."""
        with self.session_factory() as session:
            order = _paper_order_row(session, str(scope["account_id"]), order_id)
            progress = _paper_progress_row(session, str(scope["account_id"]), order_id)
        if order is None:
            return 0, None, ""
        status = str(progress["status"] if progress else order.get("status") or "")
        filled = int(
            progress["filled_quantity"]
            if progress
            else (order.get("filled_quantity") or 0)
        )
        remaining = _as_int(
            progress["remaining_quantity"] if progress else order.get("pending_quantity")
        )
        if remaining is None:
            remaining = max(int(order.get("quantity") or 0) - filled, 0)
        return filled, int(remaining), status

    async def _cancel_live(
        self, scope: Mapping[str, Any], *, order_ids: List[str], order_id: str
    ) -> Tuple[bool, int, str]:
        """Cancel known broker order ids, then VERIFY the projection is terminal."""
        wanted = [value for value in ([*order_ids] or [order_id]) if str(value or "")]
        if not wanted or self.broker_cancel is None:
            return False, 0, ""
        for value in wanted:
            try:
                await _await_maybe(
                    self.broker_cancel(
                        account_id=str(scope["account_id"]), order_id=str(value)
                    )
                )
            except Exception:  # noqa: BLE001 - an unverifiable cancel is not a cancel
                return False, 0, ""
        with self.session_factory() as session:
            projection = _broker_projection(session, str(scope["account_id"]), wanted)
        if projection is None:
            return False, 0, ""
        return (
            bool(projection["terminal"]),
            int(projection["filled_quantity"] or 0),
            str(projection["status"] or ""),
        )

    def _settle_cancelled_step(
        self,
        scope: Mapping[str, Any],
        *,
        plan_id: str,
        step_no: int,
        order_id: str,
        filled: int,
        reason: str,
        actor: str,
        key: str,
        environment: str,
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """Append the trail outcomes, settle the barrier, then move the run.

        The trail is the plan's own execution state, so the proven fill is
        recorded as ``partially_filled`` and the cancelled remainder as ``failed``
        with ``disposition=owner_cancelled`` (§1). The barrier's
        ``work_resolved`` is written only now, after the remainder is proven
        terminal, and exactly once per action key.

        Returns ``(run_status, run_id, created)``; ``created=False`` means this
        action key was already applied under the plan's own lock, so nothing was
        written and the run is left alone.
        """
        at = _utcnow()
        with self.session_factory() as session:
            self._lock_plan(session, plan_id)
            if self._disposed_already(
                session, plan_id=plan_id, step_no=int(step_no), key=key
            ):
                return None, None, False
            if filled > 0:
                session.add(
                    StrategyPlanExecutionEvent(
                        id=str(uuid.uuid4()),
                        plan_id=str(plan_id),
                        step_no=int(step_no),
                        event="partially_filled",
                        paper_order_id=str(order_id) if environment == "paper" else None,
                        broker_order_id=str(order_id) if environment == "live" else None,
                        filled_quantity=int(filled),
                        actor_id=str(actor),
                        detail={
                            "owner_action": "cancel_pending",
                            "owner_action_key": str(key),
                            "reason": str(reason or ""),
                            "preserved_fill": int(filled),
                        },
                        created_at=at,
                    )
                )
                session.flush()
            session.add(
                StrategyPlanExecutionEvent(
                    id=str(uuid.uuid4()),
                    plan_id=str(plan_id),
                    step_no=int(step_no),
                    event="failed",
                    paper_order_id=str(order_id) if environment == "paper" else None,
                    broker_order_id=str(order_id) if environment == "live" else None,
                    filled_quantity=0,
                    actor_id=str(actor),
                    detail={
                        "disposition": DISPOSITION_OWNER_CANCELLED,
                        "owner_action": "cancel_pending",
                        "owner_action_key": str(key),
                        "reason": str(reason or ""),
                        "platform_status": "cancelled",
                        "source_order_id": str(order_id),
                    },
                    # The outcome row must sort strictly after its own fill row.
                    created_at=at + timedelta(microseconds=1),
                )
            )
            session.flush()
            # Work is resolved only now: the remainder is proven terminal.
            self.barrier.record_work_event_once(
                account_id=str(scope["account_id"]),
                strategy_id=str(scope["strategy_id"]),
                execution_environment=str(environment),
                event="work_resolved",
                ref=f"plan:{plan_id}:step:{step_no}",
                detail={
                    "plan_id": str(plan_id),
                    "step_no": int(step_no),
                    "disposition": DISPOSITION_OWNER_CANCELLED,
                    "filled_quantity": int(filled),
                },
                dedupe_key=str(key),
                db=session,
            )
            session.commit()
        run_status, run_id = self._move_cancelled_entry_run(
            scope, plan_id=plan_id, actor=actor, key=key
        )
        return run_status, run_id, True

    def _move_cancelled_entry_run(
        self, scope: Mapping[str, Any], *, plan_id: str, actor: str, key: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Land the option entry run on the state its OWN fills prove (§1)."""
        with self.session_factory() as session:
            run_id = self._run_id_for(session, plan_id)
        if not run_id:
            return None, None
        from backend.options.protection.staged_exit import StagedStructureExit

        try:
            run = self._run_store().get_run(str(run_id))
        except Exception:  # noqa: BLE001 - an unreadable run is not moved
            return None, str(run_id)
        own_open = dict(StagedStructureExit.own_open_by_leg(run))
        next_run, changed = _entry_state_after_cancel(run, own_open=own_open)
        if not changed:
            return str(run.status), str(run_id)
        won = self._run_store().save_run_if_status(
            next_run, allowed_from=(str(run.status),)
        )
        if not won:
            from backend.options.execution.repair import OptionRunRepairRefusal

            raise OptionRunRepairRefusal(
                "OPTION_RUN_STATE_CHANGED",
                {
                    "option_run_id": str(run_id),
                    "observed_status": str(run.status),
                    "message": "another caller already moved this run; re-inspect",
                },
            )
        return str(next_run.status), str(run_id)

    # ------------------------------------------------- dead submissions

    def dead_submission(
        self, scope: Mapping[str, Any], *, plan: Mapping[str, Any], step_no: int
    ) -> Dict[str, Any]:
        """The evidence for ONE unanswered generic plan step (§4)."""
        return self._dead_submission_evidence(scope, plan=plan, step_no=int(step_no))

    def _dead_submission_evidence(
        self, scope: Mapping[str, Any], *, plan: Mapping[str, Any], step_no: int
    ) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        with self.session_factory() as session:
            steps = self._trail_steps(session, plan_id)
            entry = steps.get(int(step_no))
            if entry is None or not entry["events"]:
                raise OwnerActionRefusal(
                    DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE,
                    {
                        "plan_id": plan_id,
                        "step_no": int(step_no),
                        "message": "this plan step has no execution trail to dispose of",
                    },
                )
            live = self._live_claim(session, plan_id=plan_id, step_no=step_no)
            self._refuse_protective_step(session, plan=plan, step_no=step_no, live=live)
            evidence = self._dead_submission_facts(
                session, scope, plan=plan, step_no=step_no, entry=entry, live=live
            )
        evidence["evidence_digest"] = self._dead_digest(evidence)
        return evidence

    @staticmethod
    def _live_claim(session: Any, *, plan_id: str, step_no: int) -> Optional[Dict[str, Any]]:
        try:
            row = session.execute(
                select(
                    LivePlanSubmission.step_ref,
                    LivePlanSubmission.state,
                    LivePlanSubmission.broker_order_ids,
                    LivePlanSubmission.delta_snapshot,
                    LivePlanSubmission.account_id,
                    LivePlanSubmission.execution_environment,
                ).where(
                    LivePlanSubmission.plan_id == str(plan_id),
                    LivePlanSubmission.step_no == int(step_no),
                )
            ).first()
        except Exception:  # noqa: BLE001 - an unreadable claim is not an absent one
            return {"unreadable": True}
        if row is None:
            return None
        return {
            "step_ref": str(row[0] or ""),
            "state": str(row[1] or ""),
            "broker_order_ids": [str(value) for value in (row[2] or []) if str(value or "")],
            "delta_snapshot": dict(row[3] or {}),
            "account_id": str(row[4] or ""),
            "execution_environment": str(row[5] or ""),
            "unreadable": False,
        }

    @staticmethod
    def _refuse_protective_step(
        session: Any,
        *,
        plan: Mapping[str, Any],
        step_no: int,
        live: Optional[Mapping[str, Any]],
    ) -> None:
        """A staged protective exit resolves through its OWN records, never here."""
        plan_id = str(plan.get("plan_id") or "")
        rows = session.execute(
            select(StrategyPlanOptionRun.phase, StrategyPlanOptionRun.option_run_id).where(
                StrategyPlanOptionRun.plan_id == plan_id
            )
        ).all()
        exit_phases = {str(phase) for phase, _run_id in rows}
        if "exit" in exit_phases:
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN,
                {
                    "plan_id": plan_id,
                    "step_no": int(step_no),
                    "message": (
                        "this step belongs to a staged protective exit; it resolves "
                        "through the staged exit's own pre-send records and fills"
                    ),
                },
            )
        for _phase, option_run_id in rows:
            if option_run_id is None:
                continue
            state = _durable_run_state(session, str(option_run_id))
            if state is None:
                continue
            status = str(state.get("status") or "")
            if status in ("exit_previewed", "exiting", "partial_exit"):
                raise OwnerActionRefusal(
                    DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN,
                    {
                        "plan_id": plan_id,
                        "step_no": int(step_no),
                        "option_run_id": str(option_run_id),
                        "option_run_status": status,
                        "message": (
                            "the run this step belongs to is in a staged protective "
                            "exit; it resolves through the staged exit's own records"
                        ),
                    },
                )

    def _dead_submission_facts(
        self,
        session: Any,
        scope: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        step_no: int,
        entry: Mapping[str, Any],
        live: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """The platform's own facts about one step, and what they permit."""
        plan_id = str(plan.get("plan_id") or "")
        leg = _frozen_leg(plan.get("resolved_plan") or {}, step_no)
        requested = None if leg is None else _frozen_leg_quantity(leg)
        if live is not None:
            return self._live_dead_facts(
                session,
                scope,
                plan_id=plan_id,
                step_no=step_no,
                entry=entry,
                live=live,
                requested=requested,
            )
        return self._paper_dead_facts(
            session,
            scope,
            plan_id=plan_id,
            step_no=step_no,
            entry=entry,
            requested=requested,
        )

    def _paper_dead_facts(
        self,
        session: Any,
        scope: Mapping[str, Any],
        *,
        plan_id: str,
        step_no: int,
        entry: Mapping[str, Any],
        requested: Optional[int],
    ) -> Dict[str, Any]:
        account_id = str(scope["account_id"])
        order_id = entry.get("paper_order_id")
        filled = 0
        remaining = 0
        status = str(entry.get("trail_state") or "")
        source = "plan_trail"
        allowed: List[str] = []
        terminal = False
        order = None
        if not order_id:
            # A ``submitted`` row with no outcome (or a ``failed`` row whose
            # response was lost) carries no order id. The order itself is still
            # findable: every paper order is stamped with the plan and step that
            # produced it, so the platform's own attribution resolves it.
            order_id = _paper_order_for_step(session, account_id, plan_id, step_no)
        if order_id:
            order = _paper_order_row(session, account_id, str(order_id))
        if order is not None:
            progress = _paper_progress_row(session, account_id, str(order_id))
            source = "paper_order"
            status = str(progress["status"] if progress else order.get("status") or "")
            filled = int(
                progress["filled_quantity"]
                if progress
                else (order.get("filled_quantity") or 0)
            )
            remaining = _as_int(
                progress["remaining_quantity"] if progress else order.get("pending_quantity")
            )
            if remaining is None:
                remaining = max(int(order.get("quantity") or 0) - filled, 0)
            terminal = status in PAPER_TERMINAL_STATUSES
            allowed = _allowed_dispositions(
                terminal=terminal,
                status=status,
                filled=filled,
                remaining=int(remaining),
                requested=requested,
                never_submitted=False,
            )
        else:
            if not bool(entry.get("submitted")):
                # Nothing was ever attempted on this step, so there is nothing to
                # dispose of: an unanswered submission is not a dead one, and an
                # absent attempt is not a submission at all.
                raise OwnerActionRefusal(
                    DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE,
                    {
                        "plan_id": plan_id,
                        "step_no": int(step_no),
                        "message": "this step was never submitted, so it has no outcome to settle",
                    },
                )
            allowed = _allowed_dispositions(
                terminal=False,
                status=str(entry.get("trail_state") or ""),
                filled=0,
                remaining=0,
                requested=requested,
                never_submitted=True,
            )
            status = str(entry.get("trail_state") or "")
        return {
            "plan_id": plan_id,
            "step_no": int(step_no),
            "execution_environment": "paper",
            "trail_state": str(entry.get("trail_state") or ""),
            "source": source,
            "status": status,
            "order_id": None if not order_id else str(order_id),
            "requested_quantity": requested,
            "filled_quantity": int(filled),
            "remaining_quantity": int(remaining),
            "terminal": bool(terminal),
            "open_remainder": bool(not terminal and int(remaining) > 0),
            "allowed_dispositions": allowed,
        }

    def _live_dead_facts(
        self,
        session: Any,
        scope: Mapping[str, Any],
        *,
        plan_id: str,
        step_no: int,
        entry: Mapping[str, Any],
        live: Mapping[str, Any],
        requested: Optional[int],
    ) -> Dict[str, Any]:
        from backend.strategies.live_dispatch_fence import (
            FENCE_NOT_ATTEMPTED,
            FENCE_ORDER_KNOWN,
            LiveDispatchFence,
        )

        account_id = str(scope["account_id"])
        claim = dict(live)
        # The claim's OWN durable step ref is what the pre-send fence indexes on:
        # reconstructing it here could look up a ref nothing was ever sent under.
        fence = LiveDispatchFence(session_factory=self.session_factory).prove(
            account_id=account_id,
            step_ref=str(claim.get("step_ref") or ""),
            plan_id=plan_id,
            step_no=int(step_no),
        )
        state = str(fence.get("state") or "")
        order_ids = list(claim.get("broker_order_ids") or [])
        snapshot = dict(claim.get("delta_snapshot") or {})
        known_filled = int(snapshot.get("filled_quantity") or 0)
        known_remaining = _as_int(snapshot.get("remaining_quantity"))
        if known_remaining is None:
            known_remaining = (
                None if requested is None else max(requested - known_filled, 0)
            )
        if state == FENCE_ORDER_KNOWN and order_ids:
            projection = _broker_projection(session, account_id, order_ids)
            if projection is None:
                raise OwnerActionRefusal(
                    DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE,
                    {
                        "plan_id": plan_id,
                        "step_no": int(step_no),
                        "message": "the broker order projection could not be read",
                    },
                )
            status = str(projection["status"] or "")
            filled = int(projection["filled_quantity"] or 0)
            terminal = bool(projection["terminal"])
            remaining = (
                0 if terminal else (0 if known_remaining is None else int(known_remaining))
            )
            return {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "execution_environment": "live",
                "trail_state": str(entry.get("trail_state") or ""),
                "source": "broker_order",
                "status": status,
                "order_id": str(order_ids[0]),
                "requested_quantity": requested,
                "filled_quantity": filled,
                "remaining_quantity": int(remaining),
                "terminal": terminal,
                "open_remainder": bool(not terminal and int(remaining) > 0),
                "allowed_dispositions": _allowed_dispositions(
                    terminal=terminal,
                    status=status,
                    filled=filled,
                    remaining=int(remaining),
                    requested=requested,
                    never_submitted=False,
                ),
            }
        if state == FENCE_NOT_ATTEMPTED:
            return {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "execution_environment": "live",
                "trail_state": str(entry.get("trail_state") or ""),
                "source": "plan_trail",
                "status": str(claim.get("state") or ""),
                "order_id": None,
                "requested_quantity": requested,
                "filled_quantity": 0,
                "remaining_quantity": 0,
                "terminal": False,
                "open_remainder": False,
                "allowed_dispositions": _allowed_dispositions(
                    terminal=False,
                    status="",
                    filled=0,
                    remaining=0,
                    requested=requested,
                    never_submitted=True,
                ),
            }
        # A send that was attempted and never resolved: the outcome is unknown,
        # so there is nothing here an owner may dispose of.
        raise OwnerActionRefusal(
            DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE,
            {
                "plan_id": plan_id,
                "step_no": int(step_no),
                "fence_state": state,
                "fence_reason": fence.get("reason"),
                "message": (
                    "the live send was attempted and is not resolved; an unanswered "
                    "submission is not a dead one"
                ),
            },
        )

    @staticmethod
    def _dead_digest(evidence: Mapping[str, Any]) -> str:
        return _digest(
            {
                "plan_id": str(evidence.get("plan_id") or ""),
                "step_no": int(evidence.get("step_no") or 0),
                "execution_environment": str(evidence.get("execution_environment") or ""),
                "trail_state": str(evidence.get("trail_state") or ""),
                "source": str(evidence.get("source") or ""),
                "status": str(evidence.get("status") or ""),
                "order_id": evidence.get("order_id"),
                "requested_quantity": evidence.get("requested_quantity"),
                "filled_quantity": int(evidence.get("filled_quantity") or 0),
                "remaining_quantity": int(evidence.get("remaining_quantity") or 0),
                "allowed_dispositions": list(evidence.get("allowed_dispositions") or []),
            }
        )

    def dispose_dead_submission(
        self,
        scope: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        step_no: int,
        evidence_digest: str,
        disposition: str,
        reason: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Apply ONE evidence-backed disposition to ONE dead plan step (§4)."""
        evidence = self._dead_submission_evidence(scope, plan=plan, step_no=int(step_no))
        if str(evidence_digest or "") != str(evidence["evidence_digest"]):
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_EVIDENCE_CHANGED,
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "step_no": int(step_no),
                    "message": (
                        "this step's evidence changed since it was inspected; "
                        "re-inspect before disposition"
                    ),
                },
            )
        if bool(evidence.get("open_remainder")):
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_OPEN_REMAINDER,
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "step_no": int(step_no),
                    "remaining_quantity": int(evidence.get("remaining_quantity") or 0),
                    "message": (
                        "the order still has an unexecuted remainder; an unanswered "
                        "submission is not a dead one"
                    ),
                },
            )
        allowed = [str(value) for value in evidence.get("allowed_dispositions") or []]
        chosen = str(disposition or "")
        if chosen not in DEAD_SUBMISSION_DISPOSITIONS:
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_DISPOSITION_MISMATCH,
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "step_no": int(step_no),
                    "disposition": chosen,
                    "supported": list(DEAD_SUBMISSION_DISPOSITIONS),
                },
                status_code=422,
            )
        if chosen not in allowed:
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_DISPOSITION_MISMATCH,
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "step_no": int(step_no),
                    "disposition": chosen,
                    "allowed_dispositions": allowed,
                    "message": "the platform's evidence does not support this disposition",
                },
            )
        self._refuse_active_evaluator(scope)
        created = self._apply_disposition(
            scope,
            plan=plan,
            step_no=int(step_no),
            evidence=evidence,
            disposition=chosen,
            reason=reason,
            actor=actor,
        )
        if not created:
            # Another owner action already disposed of this step. The trail is
            # insert-only and the barrier is once-only, so nothing is written
            # again - and the earlier disposition is the one that stands.
            return {
                "status": STATUS_COMPLETE,
                "action_id": str(uuid.uuid4()),
                "evidence_digest": str(evidence["evidence_digest"]),
                "items": [
                    {
                        "plan_id": str(evidence["plan_id"]),
                        "step_no": int(evidence["step_no"]),
                        "order_id": evidence.get("order_id"),
                        "outcome": "already_disposed",
                        "filled_quantity": int(evidence.get("filled_quantity") or 0),
                        "remaining_quantity": 0,
                        "disposition": chosen,
                        "reason_code": None,
                    }
                ],
                "refusal": None,
                "audit_id": None,
            }
        with self.session_factory() as session:
            run_id = self._run_id_for(session, str(plan.get("plan_id") or ""))
        worker_run_id = self._worker_run_id(run_id)
        audit_id = self.record_audit(
            scope,
            action="dead_submission_disposition",
            actor=actor,
            evidence={
                "disposition": chosen,
                "evidence_digest": str(evidence["evidence_digest"]),
                "source": str(evidence.get("source") or ""),
                "platform_status": str(evidence.get("status") or ""),
                "order_id": evidence.get("order_id"),
                "filled_quantity": int(evidence.get("filled_quantity") or 0),
            },
            run_id=worker_run_id,
            option_run_id=run_id,
            plan_id=str(evidence["plan_id"]),
            step_no=int(evidence["step_no"]),
        )
        return {
            "status": STATUS_COMPLETE,
            "action_id": str(uuid.uuid4()),
            "evidence_digest": str(evidence["evidence_digest"]),
            "items": [
                {
                    "plan_id": str(evidence["plan_id"]),
                    "step_no": int(evidence["step_no"]),
                    "order_id": evidence.get("order_id"),
                    "outcome": OUTCOME_DISPOSED,
                    "filled_quantity": int(evidence.get("filled_quantity") or 0),
                    "remaining_quantity": 0,
                    "disposition": chosen,
                    "reason_code": None,
                }
            ],
            "refusal": None,
            "audit_id": audit_id,
        }

    def _refuse_active_evaluator(self, scope: Mapping[str, Any]) -> None:
        """Refuse while an active admission authority could still place work.

        The durable record of "this strategy may still act" is an ACTIVE approval
        for the strategy in this account (§4: "a step still owned by an active
        evaluator"). An unreadable read is not an absence of authority, so it
        refuses too rather than disposing of work something else may still own.
        """
        from backend.strategies.attribution_models import StrategyApproval

        try:
            with self.session_factory() as session:
                approvals = session.execute(
                    select(StrategyApproval.approval_id).where(
                        StrategyApproval.strategy_id == str(scope["strategy_id"]),
                        StrategyApproval.account_id == str(scope["account_id"]),
                        StrategyApproval.status == "active",
                    )
                ).scalars().all()
        except Exception:  # noqa: BLE001 - unreadable authority is never "absent"
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_EVALUATION_ACTIVE,
                {
                    "plan_id": "",
                    "message": (
                        "the strategy's evaluation authority could not be read; "
                        "refusing to dispose of work that may still be owned"
                    ),
                },
            )
        if approvals:
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_EVALUATION_ACTIVE,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "active_approvals": sorted(str(value) for value in approvals),
                    "message": (
                        "the strategy holds an active approval, so its evaluation "
                        "authority may still place work; stop it before disposing"
                    ),
                },
            )

    def _apply_disposition(
        self,
        scope: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        step_no: int,
        evidence: Mapping[str, Any],
        disposition: str,
        reason: str,
        actor: str,
    ) -> bool:
        """Write the terminal trail event and settle the barrier in ONE transaction.

        The event word is the fold's own terminal vocabulary
        (``filled`` / ``rejected`` / ``cancelled``) so
        ``option_plan_execution_state`` reports the plan ``finished`` afterwards
        and the adjust takeover/repair gates can proceed. The DISPOSITION - which
        is the owner's decision, not the platform's word - travels in ``detail``.

        Returns ``False`` when the action key was already applied: the whole
        decision is taken under the plan's own lock, so two concurrent
        dispositions of one step cannot both append a terminal row.
        """
        plan_id = str(plan.get("plan_id") or "")
        event, filled, refusal_reason = _trail_word_for(
            disposition,
            filled=int(evidence.get("filled_quantity") or 0),
            requested=evidence.get("requested_quantity"),
        )
        key = "dead-submission:{}:{}:{}:{}".format(
            str(scope["strategy_id"]), str(scope["account_id"]), plan_id, int(step_no)
        )
        at = _utcnow()
        with self.session_factory() as session:
            self._lock_plan(session, plan_id)
            if self._disposed_already(
                session, plan_id=plan_id, step_no=int(step_no), key=key
            ):
                return False
            session.add(
                StrategyPlanExecutionEvent(
                    id=str(uuid.uuid4()),
                    plan_id=plan_id,
                    step_no=int(step_no),
                    event=event,
                    paper_order_id=(
                        str(evidence["order_id"])
                        if evidence.get("source") == "paper_order" and evidence.get("order_id")
                        else None
                    ),
                    broker_order_id=(
                        str(evidence["order_id"])
                        if evidence.get("source") == "broker_order" and evidence.get("order_id")
                        else None
                    ),
                    filled_quantity=int(filled),
                    refusal_reason=refusal_reason,
                    actor_id=str(actor),
                    detail={
                        "disposition": str(disposition),
                        "owner_action": "dead_submission_disposition",
                        "owner_action_key": key,
                        "reason": str(reason or ""),
                        "platform_status": str(evidence.get("status") or ""),
                        "source": str(evidence.get("source") or ""),
                        "source_order_id": evidence.get("order_id"),
                    },
                    created_at=at,
                )
            )
            session.flush()
            self.barrier.record_work_event_once(
                account_id=str(scope["account_id"]),
                strategy_id=str(scope["strategy_id"]),
                execution_environment=str(
                    evidence.get("execution_environment") or scope["execution_environment"]
                ),
                event="work_resolved",
                ref=f"plan:{plan_id}:step:{int(step_no)}",
                detail={
                    "plan_id": plan_id,
                    "step_no": int(step_no),
                    "disposition": str(disposition),
                    "trail_event": event,
                },
                dedupe_key=key,
                db=session,
            )
            session.commit()
        return True

    # -------------------------------------------------------------- audit

    def record_audit(
        self,
        scope: Mapping[str, Any],
        *,
        action: str,
        actor: str,
        evidence: Mapping[str, Any],
        run_id: Optional[str] = None,
        option_run_id: Optional[str] = None,
        plan_id: str = "",
        step_no: Optional[int] = None,
    ) -> Optional[str]:
        """The owner action's audit: proposal journal + the hosted job audit.

        The journal is the strategy-scoped append-only record; the job
        reconciliation is the hosted job's own audit when a run was involved. A
        job that cannot be resolved is reported as no ``audit_id`` rather than an
        invented one.
        """
        journal_id: Optional[str] = None
        with self.session_factory() as session:
            row = StrategyProposalJournal(
                id=str(uuid.uuid4()),
                strategy_id=str(scope["strategy_id"]),
                evaluation_id=None,
                proposal_id=None,
                event="owner_action",
                reason_code=str(action),
                detail={
                    "action": str(action),
                    "actor_id": str(actor),
                    "plan_id": str(plan_id or ""),
                    "step_no": None if step_no is None else int(step_no),
                    "run_id": None if not run_id else str(run_id),
                    "option_run_id": (
                        None if not option_run_id else str(option_run_id)
                    ),
                    "evidence": dict(evidence or {}),
                },
                created_at=_utcnow(),
            )
            session.add(row)
            session.commit()
            journal_id = str(row.id)
        audit_id = self._record_job_audit(
            scope,
            action=action,
            actor=actor,
            evidence=evidence,
            run_id=run_id,
            plan_id=plan_id,
            step_no=step_no,
        )
        return audit_id or journal_id

    def _record_job_audit(
        self,
        scope: Mapping[str, Any],
        *,
        action: str,
        actor: str,
        evidence: Mapping[str, Any],
        run_id: Optional[str],
        plan_id: str,
        step_no: Optional[int],
    ) -> Optional[str]:
        if self.repository is None or not run_id:
            return None
        try:
            job = self.repository.get_job_by_run_id(str(run_id))
        except Exception:  # noqa: BLE001
            return None
        if job is None:
            return None
        try:
            row = self.repository.record_reconciliation(
                job_id=str(job.id),
                strategy_id=str(scope["strategy_id"]),
                owner_id=str(getattr(job, "owner_id", "") or ""),
                attempt=int(job.attempt or 1),
                run_id=None if not job.run_id else str(job.run_id),
                outcome="owner_action",
                reason_code=f"OWNER_ACTION_{str(action).upper()}",
                evidence={
                    "source": "hosted_owner_action",
                    "action": str(action),
                    "plan_id": str(plan_id or ""),
                    "step_no": None if step_no is None else int(step_no),
                    "run_id": str(run_id),
                    "evidence": dict(evidence or {}),
                },
                actor_id=str(actor),
            )
        except Exception:  # noqa: BLE001 - a failed audit write is not a failed action
            return None
        return str(getattr(row, "id", "") or "") or None

    # ------------------------------------------------------------- flatten

    def _flatten_store(self) -> "FlattenOperationStore":
        if self.flatten_store is None:
            self.flatten_store = FlattenOperationStore(
                session_factory=self.session_factory
            )
        return self.flatten_store

    def _snapshot_service(self) -> Any:
        if self.snapshot_service is None:
            from backend.strategies.execution_snapshot import OwnedWorkSnapshotService

            self.snapshot_service = OwnedWorkSnapshotService(
                session_factory=self.session_factory
            )
        return self.snapshot_service

    def _evaluation_authority(self, scope: Mapping[str, Any]) -> Dict[str, Any]:
        """The strategy's durable authority to still place work (§3 step 1).

        Two sources, both the platform's own: the hosted job ledger
        (``queued`` / ``starting`` / ``running``) and an ACTIVE approval for the
        strategy in this account - the record S1's dead-submission path already
        treats as evaluation authority. An unreadable read is ``None``, never an
        absence of authority, so the caller refuses instead of flattening a book
        something else may still be trading.
        """
        jobs: Optional[List[Any]] = None
        owner_id = str(scope.get("owner_id") or "")
        if self.repository is not None and owner_id:
            try:
                rows = self.repository.list_jobs_for_strategy(
                    owner_id, str(scope["strategy_id"]), limit=200
                )
            except Exception:  # noqa: BLE001 - unreadable authority is not absent
                rows = None
            if rows is not None:
                jobs = [
                    row
                    for row in rows
                    if str(getattr(row, "status", "") or "") in ACTIVE_JOB_STATUSES
                ]
        approvals: Optional[List[str]] = None
        try:
            with self.session_factory() as session:
                approvals = sorted(
                    str(value)
                    for value in session.execute(
                        select(StrategyApproval.approval_id).where(
                            StrategyApproval.strategy_id
                            == str(scope["strategy_id"]),
                            StrategyApproval.account_id
                            == str(scope["account_id"]),
                            StrategyApproval.status == "active",
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception:  # noqa: BLE001 - unreadable authority is not absent
            approvals = None
        return {"jobs": jobs, "approvals": approvals}

    def stop_evaluator(
        self,
        scope: Mapping[str, Any],
        *,
        stop_evaluator: bool,
        actor: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Stop the evaluator first, or refuse ``FLATTEN_EVALUATION_ACTIVE``.

        §3 step 1: stop, RECORD it, and only then take the snapshot flatten works
        from - a live evaluation authority could otherwise place work between the
        snapshot and the first close. The stop is proven with the operator stop
        route's own rule (a terminal label is not enough for a launched attempt
        until the supervisor confirms process cleanup), so a job that is merely
        ``stopping`` refuses rather than being assumed stopped.
        """
        if self.repository is None:
            raise OwnerActionRefusal(
                FLATTEN_EVALUATION_ACTIVE,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "message": (
                        "the hosted job ledger is not available, so the evaluator's "
                        "stop cannot be proven; refusing to flatten"
                    ),
                },
            )
        owner_id = str(scope.get("owner_id") or "")
        authority = self._evaluation_authority(scope)
        jobs = authority["jobs"]
        approvals = authority["approvals"]
        if jobs is None or approvals is None:
            raise OwnerActionRefusal(
                FLATTEN_EVALUATION_ACTIVE,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "jobs_readable": jobs is not None,
                    "approvals_readable": approvals is not None,
                    "message": (
                        "the strategy's evaluation authority could not be read, so "
                        "it cannot be proven stopped; refusing to flatten"
                    ),
                },
            )
        views: List[Dict[str, Any]] = []
        if jobs and not stop_evaluator:
            # The caller explicitly declined the stop: an active job is an
            # authority that may still trade, so flatten refuses.
            views = [_job_stop_view(job) for job in jobs]
        elif jobs:
            for job in jobs:
                status = str(getattr(job, "status", "") or "")
                try:
                    if status == "queued":
                        self.repository.stop_queued_job(
                            str(job.id),
                            owner_id=owner_id,
                            expected_attempt=int(job.attempt or 1),
                            actor=str(actor),
                        )
                    elif status in ("starting", "running"):
                        self.repository.request_stop_active(
                            str(job.id),
                            owner_id=owner_id,
                            expected_attempt=int(job.attempt or 1),
                            actor=str(actor),
                        )
                except Exception as exc:  # noqa: BLE001 - a failed stop is unproven
                    raise OwnerActionRefusal(
                        FLATTEN_EVALUATION_ACTIVE,
                        {
                            "strategy_id": str(scope["strategy_id"]),
                            "job_id": str(getattr(job, "id", "") or ""),
                            "error": type(exc).__name__,
                            "message": (
                                "the evaluator stop could not be requested; refusing "
                                "to flatten under an authority that may still act"
                            ),
                        },
                    ) from exc
                try:
                    refreshed = self.repository.get_job(owner_id, str(job.id))
                except Exception:  # noqa: BLE001 - an unreadable stop is unproven
                    refreshed = None
                views.append(_job_stop_view(refreshed or job))
        unproven = [view for view in views if not view["proven_stopped"]]
        stop = {
            "requested": bool(jobs) or bool(approvals),
            "state": "unproven" if unproven else "confirmed",
            "jobs": views,
            "approvals": list(approvals),
            "requested_by": str(actor) if jobs else None,
            "reason": str(reason or ""),
        }
        if unproven or approvals:
            # The stop itself is durable on the job row, and the refusal is
            # recorded on the strategy's own journal before it is returned: the
            # owner can see WHICH stop this flatten was gated on.
            self.record_audit(
                scope,
                action="flatten_stop_evaluator",
                actor=str(actor),
                evidence={"stop": stop, "reason": str(reason or "")},
            )
            raise OwnerActionRefusal(
                FLATTEN_EVALUATION_ACTIVE,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "stop": stop,
                    "message": (
                        "the evaluator could not be PROVEN stopped, so no new plan "
                        "may race the flatten snapshot"
                        if unproven
                        else (
                            "the strategy holds an ACTIVE approval, so its evaluation "
                            "authority may still place work"
                        )
                    ),
                },
            )
        return stop

    def _attribute_books(
        self, scope: Mapping[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """This strategy's attributed books, one per ``(instrument, product)``.

        ``None`` is "unreadable", never "flat": flatten may not report a book
        closed from a read it could not make. Canonical rows are grouped by their
        canonical instrument and product; ``raw`` rows are unresolved exposure
        whose identity the platform cannot attribute, and they are reported as
        their own entries so the caller refuses to call the book flat.
        """
        try:
            with self.session_factory() as session:
                rows = session.execute(
                    select(
                        StrategyPositionProjection.identity_kind,
                        StrategyPositionProjection.identity_key,
                        StrategyPositionProjection.canonical_instrument_id,
                        StrategyPositionProjection.instrument_token,
                        StrategyPositionProjection.exchange,
                        StrategyPositionProjection.tradingsymbol,
                        StrategyPositionProjection.product,
                        StrategyPositionProjection.net_quantity,
                    ).where(
                        StrategyPositionProjection.account_id
                        == str(scope["account_id"]),
                        StrategyPositionProjection.strategy_id
                        == str(scope["strategy_id"]),
                        StrategyPositionProjection.execution_environment
                        == str(scope["execution_environment"]),
                    )
                ).all()
        except Exception:  # noqa: BLE001 - an unreadable book is not a flat one
            return None
        grouped: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            identity_kind = str(row[0] or "")
            if identity_kind == "canonical":
                key = ("canonical", str(row[2] or ""), str(row[6] or ""))
                entry = grouped.setdefault(
                    key,
                    {
                        "identity_kind": "canonical",
                        "identity_key": str(row[2] or ""),
                        "instrument_id": str(row[2] or ""),
                        "instrument_token": int(row[3] or 0),
                        "exchange": str(row[4] or ""),
                        "tradingsymbol": str(row[5] or ""),
                        "product": str(row[6] or ""),
                        "net_quantity": 0,
                    },
                )
            else:
                key = ("raw", str(row[1] or ""), str(row[6] or ""))
                entry = grouped.setdefault(
                    key,
                    {
                        "identity_kind": "raw",
                        "identity_key": str(row[1] or ""),
                        "instrument_id": None,
                        "instrument_token": int(row[3] or 0),
                        "exchange": str(row[4] or ""),
                        "tradingsymbol": str(row[5] or ""),
                        "product": str(row[6] or ""),
                        "net_quantity": 0,
                    },
                )
            entry["net_quantity"] = int(entry["net_quantity"]) + int(row[7] or 0)
        return [grouped[key] for key in sorted(grouped)]

    def _book_quantity(
        self, scope: Mapping[str, Any], *, instrument_id: str, product: str
    ) -> Optional[int]:
        """This strategy's attributed quantity for ONE book, or ``None``.

        ``None`` is "unreadable", never zero: a reduction whose resulting book
        cannot be read is not reported as flat.
        """
        if not instrument_id:
            return None
        try:
            with self.session_factory() as session:
                rows = session.execute(
                    select(StrategyPositionProjection.net_quantity).where(
                        StrategyPositionProjection.account_id
                        == str(scope["account_id"]),
                        StrategyPositionProjection.strategy_id
                        == str(scope["strategy_id"]),
                        StrategyPositionProjection.execution_environment
                        == str(scope["execution_environment"]),
                        StrategyPositionProjection.identity_kind == "canonical",
                        StrategyPositionProjection.canonical_instrument_id
                        == str(instrument_id),
                        StrategyPositionProjection.product == str(product),
                    )
                ).scalars().all()
        except Exception:  # noqa: BLE001 - an unreadable book is not a flat one
            return None
        return int(sum(int(value or 0) for value in rows))

    def _owner_cancelled_steps(self, scope: Mapping[str, Any]) -> set:
        """``(plan_id, step_no)`` this strategy's OWN cancel already settled.

        Section 1 mandates the disposition word: a cancelled remainder is written
        as ``failed`` with ``disposition=owner_cancelled``. That word is NOT in the
        plan-execution fold's terminal vocabulary (the fold reports such a plan
        ``unknown`` rather than finished), so a reader that only looked at the fold
        would keep re-reporting work the owner already disbanded - and flatten
        would ask the owner to disposition a remainder the platform itself
        cancelled. This reader is the missing half: the disposition, its preserved
        fill and the terminal order ARE the outcome.
        """
        out: set = set()
        try:
            plans = self._plan_rows(scope)
            with self.session_factory() as session:
                for plan in plans:
                    rows = session.execute(
                        select(
                            StrategyPlanExecutionEvent.step_no,
                            StrategyPlanExecutionEvent.detail,
                        ).where(
                            StrategyPlanExecutionEvent.plan_id
                            == str(plan["plan_id"])
                        )
                    ).all()
                    for step_no, detail in rows:
                        payload = dict(detail or {})
                        if (
                            str(payload.get("disposition") or "")
                            != DISPOSITION_OWNER_CANCELLED
                        ):
                            continue
                        out.add((str(plan["plan_id"]), int(step_no or 0)))
        except Exception:  # noqa: BLE001 - an unreadable trail is not "settled"
            return set()
        return out

    def _instrument_types(
        self, instrument_ids: Sequence[str]
    ) -> Optional[Dict[str, str]]:
        """The canonical ``instrument_type`` of each book (§3 step 5).

        A book whose catalog record cannot be read is NOT classified by guessing:
        ``None`` means the whole read failed, and a missing entry means this one
        instrument's type is unknown - either refuses the item by name.
        """
        wanted = sorted({str(value) for value in instrument_ids if str(value or "")})
        if not wanted:
            return {}
        from sqlalchemy import bindparam

        try:
            with self.session_factory() as session:
                rows = session.execute(
                    text(
                        "SELECT instrument_id, COALESCE(instrument_type, '') "
                        "FROM public.instrument_catalog_records "
                        "WHERE instrument_id IN :ids"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": wanted},
                ).all()
        except Exception:  # noqa: BLE001 - an unreadable catalog is not "not an option"
            return None
        return {str(row[0]): str(row[1] or "") for row in rows}

    def _outstanding_plan_steps(
        self, scope: Mapping[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Every submitted plan step whose trail has no terminal outcome yet.

        ``None`` means a plan's own trail blocks could not be read, which is
        in-flight evidence the platform cannot dismiss. A step this strategy's
        owner already cancelled is excluded: its disposition IS its outcome (see
        ``_owner_cancelled_steps``).
        """
        out: List[Dict[str, Any]] = []
        settled = self._owner_cancelled_steps(scope)
        try:
            plans = self._plan_rows(scope)
            with self.session_factory() as session:
                for plan in plans:
                    steps = self._trail_steps(session, plan["plan_id"])
                    for step_no, entry in steps.items():
                        if not entry["submitted"]:
                            continue
                        if (str(plan["plan_id"]), int(step_no)) in settled:
                            continue
                        if any(
                            event in FOLD_TERMINAL_EVENTS for event in entry["events"]
                        ):
                            continue
                        out.append(
                            {
                                "plan_id": str(plan["plan_id"]),
                                "step_no": int(step_no),
                                "state": str(entry["trail_state"]),
                                "order_id": entry.get("paper_order_id")
                                or entry.get("broker_order_id"),
                            }
                        )
        except Exception:  # noqa: BLE001 - an unreadable trail is not quiet evidence
            return None
        out.sort(key=lambda row: (row["plan_id"], row["step_no"]))
        return out

    def _flatten_preflight(self, scope: Mapping[str, Any]) -> Dict[str, Any]:
        """Work flatten must not GUESS about (§3 step 2).

        The preview's own verdict decides: an INELIGIBLE candidate with no open
        remainder is a dead submission the owner must disposition by name, while
        one whose remainder is still open is in-flight work that resolves by
        filling (flatten leaves it alone and waits). An option run whose own
        durable records still own an unresolved protective stage is the third
        source: it resolves through the staged exit's own pre-send records.
        """
        preview = self.preview_pending(scope)
        settled = self._owner_cancelled_steps(scope)
        dead: List[Dict[str, Any]] = []
        waiting: List[Dict[str, Any]] = []
        for candidate in preview["items"]:
            if str(candidate.get("eligibility")) == ELIGIBLE:
                continue
            if (str(candidate["plan_id"]), int(candidate["step_no"])) in settled:
                # This strategy's own cancel already disbanded the remainder:
                # there is nothing left to wait for and nothing to disposition.
                continue
            remaining = int(candidate.get("remaining_quantity") or 0)
            entry = {
                "plan_id": str(candidate["plan_id"]),
                "step_no": int(candidate["step_no"]),
                "order_id": candidate.get("order_id"),
                "environment": str(candidate.get("environment") or ""),
                "remaining_quantity": remaining,
                "reason_code": candidate.get("reason_code"),
                "disposition_url": (
                    f"/api/strategies/{str(scope['strategy_id'])}/plans/"
                    f"{str(candidate['plan_id'])}/steps/{int(candidate['step_no'])}/"
                    "dead-submission"
                ),
            }
            (dead if remaining <= 0 else waiting).append(entry)
        rows, coverage = self.option_runs_for_scope(scope)
        protective: List[Dict[str, Any]] = []
        for row in rows:
            if not bool(row.get("protective_exit_unresolved")):
                continue
            option_run_id = str(row.get("option_run_id") or "")
            protective.append(
                {
                    "option_run_id": option_run_id,
                    "status": str(row.get("status") or ""),
                    "exit_url": (
                        f"/api/strategies/{str(scope['strategy_id'])}/option-runs/"
                        f"{option_run_id}/exit"
                    ),
                }
            )
        return {
            "dead": dead,
            "waiting": waiting,
            "protective_stages": protective,
            "preview_digest": str(preview["evidence_digest"]),
            "coverage": str(coverage.get("coverage") or COVERAGE_UNKNOWN),
        }

    def option_runs_for_scope(
        self, scope: Mapping[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """This strategy's own option runs, plus the read's coverage verdict."""
        try:
            rows, coverage = self._snapshot_service().option_runs_for_scope(
                account_id=str(scope["account_id"]),
                strategy_id=str(scope["strategy_id"]),
                environment=str(scope["execution_environment"]),
            )
        except Exception:  # noqa: BLE001 - an unreadable run set is not an empty one
            return [], {
                "coverage": COVERAGE_UNKNOWN,
                "reason": "option_run_read_failed",
            }
        return list(rows or []), dict(coverage or {})

    async def _flatten_cancel_items(
        self, scope: Mapping[str, Any], *, reason: str, actor: str
    ) -> List[Dict[str, Any]]:
        """Cancel only what the S1 preview proves eligible (§3 step 3).

        One classifier, not a second one: the cancel is pinned to the preview's
        own digest, and a candidate the classifier refuses is reported with its
        named reason instead of being cancelled.
        """
        preview = self.preview_pending(scope)
        items: List[Dict[str, Any]] = []
        settled = self._owner_cancelled_steps(scope)
        if any(str(row.get("eligibility")) == ELIGIBLE for row in preview["items"]):
            result = await self.cancel_pending(
                scope,
                evidence_digest=str(preview["evidence_digest"]),
                reason=str(reason or ""),
                actor=str(actor),
            )
            for row in result.get("items") or []:
                outcome = str(row.get("outcome") or "")
                done = outcome in (OUTCOME_CANCELLED, OUTCOME_ALREADY_CANCELLED)
                remaining = int(row.get("remaining_quantity") or 0)
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_CANCEL,
                        f"cancel:{row['plan_id']}:{int(row['step_no'])}",
                        # An item the classifier refused is left ALONE: while its
                        # remainder is open it is working (``in_progress``), and
                        # only an unprovable, already-stopped one is ``blocked``.
                        ITEM_STATE_DONE
                        if done
                        else (
                            ITEM_STATE_IN_PROGRESS
                            if remaining > 0
                            else ITEM_STATE_BLOCKED
                        ),
                        reason_code=None if done else row.get("reason_code"),
                        detail={
                            "plan_id": str(row["plan_id"]),
                            "step_no": int(row["step_no"]),
                            "order_id": row.get("order_id"),
                            "outcome": outcome,
                            "filled_quantity": int(row.get("filled_quantity") or 0),
                            "remaining_quantity": remaining,
                            "disposition": row.get("disposition"),
                            "run_status": row.get("run_status"),
                        },
                    )
                )
        for row in preview["items"]:
            if str(row.get("eligibility")) == ELIGIBLE:
                continue
            key = f"cancel:{row['plan_id']}:{int(row['step_no'])}"
            if any(item["key"] == key for item in items):
                continue
            if (str(row["plan_id"]), int(row["step_no"])) in settled:
                continue
            # Defensive: the preflight refuses dead submissions before this runs,
            # so an ineligible candidate here is work flatten must leave ALONE -
            # a reducing or protective order that is still working (``in_progress``
            # until its own fills settle), or a RACE that moved the evidence into
            # an unprovable state (``blocked`` by name). It is never cancelled.
            remaining = int(row.get("remaining_quantity") or 0)
            items.append(
                _flatten_item(
                    FLATTEN_ITEM_CANCEL,
                    key,
                    ITEM_STATE_IN_PROGRESS if remaining > 0 else ITEM_STATE_BLOCKED,
                    reason_code=str(row.get("reason_code") or CANCEL_ORDER_NOT_OWNED),
                    detail={
                        "plan_id": str(row["plan_id"]),
                        "step_no": int(row["step_no"]),
                        "order_id": row.get("order_id"),
                        "eligibility": str(row.get("eligibility") or ""),
                        "remaining_quantity": remaining,
                    },
                )
            )
        return items

    async def _flatten_option_exit_items(
        self, scope: Mapping[str, Any], *, reason: str
    ) -> List[Dict[str, Any]]:
        """Exit option runs ONE AT A TIME through the S2 path (§3 step 4).

        Each run's own fills prove its hedge release, so legs are never merged
        across runs. The pass stops at the first run that is not finished: its
        staged exit is WAITING on fills, and starting the next run would trade a
        structure whose hedge proof belongs to the first one.
        """
        rows, coverage = self.option_runs_for_scope(scope)
        items: List[Dict[str, Any]] = []
        if not rows:
            return items
        if str(coverage.get("coverage") or COVERAGE_UNKNOWN) != COVERAGE_KNOWN:
            items.append(
                _flatten_item(
                    FLATTEN_ITEM_OPTION_EXIT,
                    "option_exit:scope",
                    ITEM_STATE_BLOCKED,
                    reason_code=FLATTEN_OPTION_RUN_COVERAGE_UNKNOWN,
                    detail={"reason": str(coverage.get("reason") or "")},
                )
            )
            return items
        for row in rows:
            option_run_id = str(row.get("option_run_id") or "")
            if not option_run_id:
                continue
            key = f"option_exit:{option_run_id}"
            try:
                result = await self._run_option_exit(
                    scope, option_run_id=option_run_id, reason=str(reason or "")
                )
            except OwnerActionRefusal as exc:
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_OPTION_EXIT,
                        key,
                        ITEM_STATE_BLOCKED,
                        reason_code=exc.reason_code,
                        detail=dict(exc.detail or {}),
                    )
                )
                break
            except OptionRunRepairRefusal as exc:
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_OPTION_EXIT,
                        key,
                        ITEM_STATE_BLOCKED,
                        reason_code=str(exc.reason_code),
                        detail=exc.as_detail(),
                    )
                )
                break
            except HTTPException as exc:
                detail = (
                    dict(exc.detail)
                    if isinstance(exc.detail, Mapping)
                    else {"message": str(exc.detail)}
                )
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_OPTION_EXIT,
                        key,
                        ITEM_STATE_BLOCKED,
                        reason_code=str(
                            detail.get("rejection_reason")
                            or "OPTION_OWNER_EXIT_BOUNDARY_UNAVAILABLE"
                        ),
                        detail=detail,
                    )
                )
                break
            state = str(result.get("state") or "")
            run_status = str(result.get("run_status") or "")
            detail = {
                "option_run_id": option_run_id,
                "state": state,
                "run_status": run_status,
                "evidence_digest": str(result.get("evidence_digest") or ""),
                "shorts_proven_closed": bool(result.get("shorts_proven_closed")),
                "withheld_hedges": list(result.get("withheld_hedges") or []),
                "stage_items": list(result.get("items") or []),
            }
            if state == STATE_FLAT and run_status in TERMINAL_RUN_STATUSES:
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_OPTION_EXIT, key, ITEM_STATE_DONE, detail=detail
                    )
                )
                continue
            if str(result.get("status") or "") == "accepted":
                # ONE stage was submitted; the run's own fills decide the next
                # stage, so this item is waiting, not done.
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_OPTION_EXIT,
                        key,
                        ITEM_STATE_IN_PROGRESS,
                        reason_code="option_exit_stage_submitted",
                        detail=detail,
                    )
                )
            else:
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_OPTION_EXIT,
                        key,
                        ITEM_STATE_BLOCKED,
                        reason_code=str(
                            result.get("refusal")
                            or "OPTION_OWNER_EXIT_STAGE_NOT_SUBMITTED"
                        ),
                        detail=detail,
                    )
                )
            # ONE AT A TIME: this run is not finished, so no later run starts
            # until the next pass - its hedge proof is its own.
            break
        return items

    async def _run_option_exit(
        self, scope: Mapping[str, Any], *, option_run_id: str, reason: str
    ) -> Dict[str, Any]:
        runner = self.option_exit_runner
        if runner is None:
            raise OwnerActionRefusal(
                "OPTION_OWNER_EXIT_BOUNDARY_UNAVAILABLE",
                {
                    "option_run_id": str(option_run_id),
                    "message": "no owner-exit boundary is wired for this deployment",
                },
            )
        result = await _await_maybe(
            runner(scope, str(option_run_id), reason=str(reason or ""))
        )
        return dict(result or {})

    async def _flatten_reduction_items(
        self,
        scope: Mapping[str, Any],
        *,
        operation_id: str,
        actor: str,
    ) -> List[Dict[str, Any]]:
        """Close the strategy's non-option books with target-zero plans (§3.5).

        One frozen plan per ``(instrument, product)``: the frozen target is ZERO
        and the executor derives the order from the strategy's own attributed
        book, so the plan can only reduce. A plan that would increase exposure is
        refused BEFORE admission. Live and paper books take the SAME governed
        path: the pipeline dispatches to the environment's own executor, and a
        reduction is never lane/market-hours/loss gated by the live executor.
        """
        books = self._attribute_books(scope)
        if books is None:
            return [
                _flatten_item(
                    FLATTEN_ITEM_REDUCTION,
                    "reduction:books",
                    ITEM_STATE_BLOCKED,
                    reason_code="FLATTEN_BOOKS_UNREADABLE",
                    detail={},
                )
            ]
        nonzero = [book for book in books if int(book["net_quantity"] or 0) != 0]
        types = self._instrument_types(
            [str(book.get("instrument_id") or "") for book in nonzero]
        )
        items: List[Dict[str, Any]] = []
        for book in nonzero:
            key = (
                f"reduction:raw:{book.get('identity_key')}:{book.get('product')}"
                if str(book.get("identity_kind")) != "canonical"
                else f"reduction:{book.get('instrument_id')}:{book.get('product')}"
            )
            if str(book.get("identity_kind")) != "canonical":
                # Unresolved exposure: the platform cannot say WHICH instrument it
                # is, so it will not flush it through a canonical plan.
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_REDUCTION,
                        key,
                        ITEM_STATE_BLOCKED,
                        reason_code=FLATTEN_UNATTRIBUTED_EXPOSURE,
                        detail=dict(book),
                    )
                )
                continue
            instrument_type = (
                None
                if types is None
                else types.get(str(book.get("instrument_id") or ""))
            )
            if instrument_type in OPTION_INSTRUMENT_TYPES:
                # Options are closed by their OWN run's staged exit; a
                # single-instrument reduction here would merge structures.
                continue
            if types is None or not instrument_type:
                items.append(
                    _flatten_item(
                        FLATTEN_ITEM_REDUCTION,
                        key,
                        ITEM_STATE_BLOCKED,
                        reason_code=FLATTEN_REDUCTION_INSTRUMENT_UNKNOWN,
                        detail=dict(book),
                    )
                )
                continue
            items.append(
                await self._close_non_option_book(
                    scope,
                    book=book,
                    key=key,
                    operation_id=str(operation_id),
                    actor=str(actor),
                )
            )
        return items

    async def _close_non_option_book(
        self,
        scope: Mapping[str, Any],
        *,
        book: Mapping[str, Any],
        key: str,
        operation_id: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Build, gate, admit and execute ONE target-zero reduction plan."""
        attributed_open = int(book.get("net_quantity") or 0)
        try:
            built = dict(
                await _await_maybe(
                    self.reduction_plan_builder(
                        scope,
                        dict(book),
                        operation_id=str(operation_id),
                        actor=str(actor),
                    )
                )
                or {}
            )
        except OwnerActionRefusal as exc:
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=exc.reason_code,
                detail=dict(exc.detail or {}),
            )
        except Exception as exc:  # noqa: BLE001 - an unbuildable plan cannot run
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=FLATTEN_REDUCTION_PLAN_REFUSED,
                detail={"error": type(exc).__name__, "message": str(exc)},
            )
        plan = dict(built.get("plan") or {})
        detail: Dict[str, Any] = {
            "instrument_id": book.get("instrument_id"),
            "product": book.get("product"),
            "attributed_open_quantity": int(attributed_open),
            "plan_id": str(built.get("plan_id") or plan.get("plan_id") or ""),
            "target_quantity": 0,
        }
        if not plan:
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=str(
                    built.get("reason_code") or FLATTEN_REDUCTION_PLAN_REFUSED
                ),
                detail={**detail, "refusal": built.get("refusal")},
            )
        if plan_increases_exposure(plan, attributed_open=int(attributed_open)):
            # BEFORE admission: the platform never admits a flatten plan whose
            # frozen target would grow or reverse this book.
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=FLATTEN_PLAN_INCREASES_EXPOSURE,
                detail=detail,
            )
        pipeline = self.reduction_pipeline
        if pipeline is None:
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=FLATTEN_REDUCTION_PIPELINE_UNAVAILABLE,
                detail=detail,
            )
        environment = str(scope["execution_environment"])
        try:
            verdict = dict(
                await _await_maybe(
                    pipeline.admit(plan, environment=environment)
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001 - an unadmitted plan is not executed
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code="ADMISSION_REFUSED",
                detail={
                    **detail,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
        if not bool(verdict.get("admitted")):
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=str(
                    verdict.get("reason_code")
                    or verdict.get("rejection_reason")
                    or verdict.get("reason")
                    or "ADMISSION_REFUSED"
                ),
                detail={**detail, "admission": verdict},
            )
        detail["admission"] = verdict
        try:
            result = dict(
                await _await_maybe(pipeline.execute(plan, actor=str(actor))) or {}
            )
        except Exception as exc:  # noqa: BLE001 - a failed execution blocks the item
            return _flatten_item(
                FLATTEN_ITEM_REDUCTION,
                key,
                ITEM_STATE_BLOCKED,
                reason_code=str(
                    getattr(exc, "reason_code", "") or "FLATTEN_REDUCTION_FAILED"
                ),
                detail={
                    **detail,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
        status = str(result.get("status") or "")
        # The COMMAND's word is not the book's state: a reduction is done only
        # when the strategy's own attributed quantity for this book is ZERO
        # afterwards (a coarse ``no_op``/partial pass is not proof of flat).
        remaining = self._book_quantity(
            scope,
            instrument_id=str(book.get("instrument_id") or ""),
            product=str(book.get("product") or ""),
        )
        done = status in ("filled", "no_op") and remaining == 0
        detail["execution"] = {
            "status": status,
            "steps": list(result.get("steps") or []),
        }
        detail["remaining_quantity"] = remaining
        return _flatten_item(
            FLATTEN_ITEM_REDUCTION,
            key,
            ITEM_STATE_DONE if done else ITEM_STATE_BLOCKED,
            reason_code=None if done else "FLATTEN_REDUCTION_INCOMPLETE",
            detail=detail,
        )

    def _done_conditions(self, scope: Mapping[str, Any]) -> Dict[str, Any]:
        """§3's done conditions, each with its own evidence.

        ``complete`` requires ALL of them, so a condition the platform cannot
        check is reported as unmet with its named reason rather than assumed.
        """
        out: Dict[str, Any] = {}
        preview = self.preview_pending(scope)
        eligible = [
            row
            for row in preview["items"]
            if str(row.get("eligibility")) == ELIGIBLE
        ]
        out[DONE_NO_PENDING_ENTRY] = {
            "satisfied": not eligible,
            "evidence": {"eligible": len(eligible)},
        }
        outstanding = self._outstanding_plan_steps(scope)
        live_claims: Optional[List[Dict[str, Any]]] = None
        try:
            with self.session_factory() as session:
                rows = self._live_claim_rows(session, scope)
            live_claims = None if rows is None else [dict(row) for row in rows]
        except Exception:  # noqa: BLE001 - an unreadable claim set is not quiet
            live_claims = None
        out[DONE_NO_LIVE_UNRESOLVED] = {
            "satisfied": live_claims is not None and not live_claims,
            "evidence": {
                "unreadable": live_claims is None,
                "claims": list(live_claims or []),
            },
        }
        runs, coverage = self.option_runs_for_scope(scope)
        run_rows: List[Dict[str, Any]] = []
        runs_flat = True
        for row in runs:
            option_run_id = str(row.get("option_run_id") or "")
            status = str(row.get("status") or "")
            state = "unknown"
            try:
                run = self._run_store().get_run(option_run_id)
                from backend.options.protection.staged_exit import StagedStructureExit

                own_open = StagedStructureExit.own_open_by_leg(run)
                state = (
                    "flat"
                    if all(int(value or 0) == 0 for value in own_open.values())
                    else "residual"
                )
            except Exception:  # noqa: BLE001 - an unreadable run is not flat
                state = "unknown"
            flat_and_terminal = state == "flat" and status in TERMINAL_RUN_STATUSES
            runs_flat = runs_flat and flat_and_terminal
            run_rows.append(
                {
                    "option_run_id": option_run_id,
                    "status": status,
                    "state": state,
                    "protective_exit_unresolved": bool(
                        row.get("protective_exit_unresolved")
                    ),
                }
            )
        coverage_known = (
            str(coverage.get("coverage") or COVERAGE_UNKNOWN) == COVERAGE_KNOWN
        )
        out[DONE_OPTION_RUNS_FLAT] = {
            "satisfied": bool(coverage_known and runs_flat),
            "evidence": {
                "coverage": str(coverage.get("coverage") or COVERAGE_UNKNOWN),
                "coverage_reason": str(coverage.get("reason") or ""),
                "runs": run_rows,
            },
        }
        books = self._attribute_books(scope)
        nonzero = (
            None
            if books is None
            else [
                {
                    "instrument_id": book.get("instrument_id"),
                    "identity_kind": book.get("identity_kind"),
                    "product": book.get("product"),
                    "tradingsymbol": book.get("tradingsymbol"),
                    "net_quantity": int(book.get("net_quantity") or 0),
                }
                for book in books
                if int(book.get("net_quantity") or 0) != 0
            ]
        )
        out[DONE_BOOKS_ZERO] = {
            "satisfied": nonzero is not None and not nonzero,
            "evidence": {
                "unreadable": nonzero is None,
                "nonzero": list(nonzero or []),
            },
        }
        authority = self._evaluation_authority(scope)
        running = (
            None
            if authority["jobs"] is None
            else [_job_stop_view(job) for job in authority["jobs"]]
        )
        out[DONE_NO_EVALUATION_AUTHORITY] = {
            "satisfied": bool(
                running is not None
                and not running
                and authority["approvals"] is not None
                and not authority["approvals"]
            ),
            "evidence": {
                "jobs": list(running or []),
                "jobs_readable": running is not None,
                "approvals": list(authority["approvals"] or []),
                "approvals_readable": authority["approvals"] is not None,
            },
        }
        in_flight = {
            "outstanding_plan_steps": outstanding,
            "option_runs": [
                row
                for row in run_rows
                if row["state"] != "flat"
                or row["status"] not in TERMINAL_RUN_STATUSES
                or row["protective_exit_unresolved"]
            ],
            "live_unresolved_claims": live_claims,
        }
        out[DONE_NO_INFLIGHT_WORK] = {
            "satisfied": bool(
                outstanding is not None
                and not outstanding
                and not in_flight["option_runs"]
                and live_claims is not None
                and not live_claims
            ),
            "evidence": in_flight,
        }
        return out

    async def flatten(
        self,
        scope: Mapping[str, Any],
        *,
        reason: str,
        stop_evaluator: bool,
        actor: str,
    ) -> Dict[str, Any]:
        """The §3 orchestration: stop, preflight, cancel, exit runs, close books.

        The operation is durable BEFORE any work moves, and a repeated POST
        RESUMES it: outcomes already recorded are merged into the freshly derived
        manifest, so completed reductions are preserved and only what is left is
        attempted again.
        """
        stop = self.stop_evaluator(
            scope,
            stop_evaluator=bool(stop_evaluator),
            actor=str(actor),
            reason=str(reason or ""),
        )
        preflight = self._flatten_preflight(scope)
        if preflight["dead"] or preflight["protective_stages"]:
            raise OwnerActionRefusal(
                DEAD_SUBMISSION_UNRESOLVED,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "steps": preflight["dead"],
                    "protective_stages": preflight["protective_stages"],
                    "waiting": preflight["waiting"],
                    "message": (
                        "this strategy has unanswered work the platform cannot "
                        "resolve on its own; disposition it by name before flattening"
                    ),
                },
            )
        store = self._flatten_store()
        operation = store.open(scope)
        if operation is None:
            operation = store.create(
                scope,
                operation_id=str(uuid.uuid4()),
                actor=str(actor),
                reason=str(reason or ""),
                stop=stop,
                status=STATUS_IN_PROGRESS,
            )
        else:
            operation = store.save(
                str(operation["operation_id"]),
                status=STATUS_IN_PROGRESS,
                stop=stop,
                reason=str(reason or ""),
                actor=str(actor),
            )
        previous = {
            str(item.get("key") or ""): dict(item)
            for item in (operation.get("items") or [])
        }
        items: List[Dict[str, Any]] = []
        items.extend(
            await self._flatten_cancel_items(
                scope, reason=str(reason or ""), actor=str(actor)
            )
        )
        items.extend(
            await self._flatten_option_exit_items(scope, reason=str(reason or ""))
        )
        items.extend(
            await self._flatten_reduction_items(
                scope,
                operation_id=str(operation["operation_id"]),
                actor=str(actor),
            )
        )
        merged = _merge_flatten_items(previous, items)
        done = self._done_conditions(scope)
        missing = [name for name, value in done.items() if not value["satisfied"]]
        blocked = [
            row for row in merged if str(row.get("state")) == ITEM_STATE_BLOCKED
        ]
        refusal = str(blocked[0].get("reason_code") or "") if blocked else None
        if not missing:
            status = STATUS_COMPLETE
        elif blocked:
            status = STATUS_BLOCKED
        else:
            status = STATUS_IN_PROGRESS
        evidence_digest = _digest(
            {
                "strategy_id": str(scope["strategy_id"]),
                "account_id": str(scope["account_id"]),
                "execution_environment": str(scope["execution_environment"]),
                "operation_id": str(operation["operation_id"]),
                "stop": stop,
                "items": merged,
                "done": {name: value["satisfied"] for name, value in done.items()},
            }
        )
        saved = store.save(
            str(operation["operation_id"]),
            status=status,
            manifest={"items": merged},
            evidence_digest=evidence_digest,
            refusal=refusal,
        )
        audit_id = self.record_audit(
            scope,
            action="flatten",
            actor=str(actor),
            evidence={
                "operation_id": str(saved["operation_id"]),
                "reason": str(reason or ""),
                "stop": stop,
                "items": merged,
                "missing": missing,
                "refusal": refusal,
            },
        )
        return _flatten_response(saved, done=done, missing=missing, audit_id=audit_id)

    def flatten_status(self, scope: Mapping[str, Any]) -> Dict[str, Any]:
        """The latest flatten operation for this scope, with the CURRENT verdict.

        Read-only: the item outcomes are the stored ones, while the done
        conditions (and therefore ``complete``) are re-derived from live evidence,
        so a status read never reports a stale "still working" or a stale "done"
        for a book that has moved since.
        """
        operation = self._flatten_store().latest(scope)
        if operation is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "rejection_reason": "FLATTEN_OPERATION_NONE",
                    "strategy_id": str(scope["strategy_id"]),
                    "message": "this strategy has no flatten operation to resume",
                },
            )
        done = self._done_conditions(scope)
        missing = [name for name, value in done.items() if not value["satisfied"]]
        blocked = [
            row
            for row in (operation.get("items") or [])
            if str(row.get("state")) == ITEM_STATE_BLOCKED
        ]
        if not missing:
            status = STATUS_COMPLETE
        elif blocked:
            status = STATUS_BLOCKED
        else:
            status = STATUS_IN_PROGRESS
        operation = dict(operation)
        operation["status"] = status
        return _flatten_response(
            operation, done=done, missing=missing, audit_id=None
        )


# ---------------------------------------------------------------------------
# module-level rules
# ---------------------------------------------------------------------------


def _allowed_dispositions(
    *,
    terminal: bool,
    status: str,
    filled: int,
    remaining: int,
    requested: Optional[int],
    never_submitted: bool,
) -> List[str]:
    """Which §4 dispositions the platform's evidence actually supports.

    Each disposition is permitted only by its own evidence:

    * ``filled`` - proven fills cover the full requested quantity;
    * ``rejected`` - a terminal rejection with nothing filled;
    * ``cancelled`` - a terminal cancellation, with the proven fill preserved;
    * ``failed_never_submitted`` - durable records prove the step never reached
      the order path;
    * ``failed_residual_abandoned`` - a terminal order with zero remaining, after
      every proven fill is recorded.
    """
    status = str(status or "").strip().lower()
    allowed: List[str] = []
    if never_submitted:
        return ["failed_never_submitted"]
    if not terminal:
        return allowed
    if (
        status == "filled"
        and requested is not None
        and requested > 0
        and int(filled) >= int(requested)
    ):
        allowed.append("filled")
    if status == "rejected" and int(filled) == 0:
        allowed.append("rejected")
    if status == "cancelled":
        allowed.append("cancelled")
    if int(remaining) == 0 and (allowed or status not in ("filled", "rejected", "cancelled")):
        allowed.append("failed_residual_abandoned")
    return [value for value in DEAD_SUBMISSION_DISPOSITIONS if value in allowed]


def _trail_word_for(
    disposition: str, *, filled: int, requested: Optional[int]
) -> Tuple[str, int, Optional[str]]:
    """The trail event one disposition writes: ``(event, filled, refusal_reason)``.

    The event word is the plan-execution fold's own terminal vocabulary, because
    that fold - not this action - is what later tells the adjust takeover and
    repair paths that the plan finished. The owner's disposition is carried in
    ``detail``; the two are deliberately not conflated.
    """
    disposition = str(disposition)
    if disposition == "filled":
        return "filled", int(filled if filled > 0 else (requested or 0)), None
    if disposition == "rejected":
        return "rejected", 0, "DEAD_SUBMISSION_REJECTED"
    if disposition == "cancelled":
        return "cancelled", int(filled), None
    if disposition == "failed_never_submitted":
        # A step that never reached the order path is recorded with the word the
        # executor already uses for a refusal that was never committed
        # (``rejected`` with no order): that is what closes the step for the fold.
        return "rejected", 0, "DEAD_SUBMISSION_NEVER_SUBMITTED"
    return "cancelled", int(filled), None


def _entry_state_after_cancel(run: Any, *, own_open: Mapping[str, Any]) -> Tuple[Any, bool]:
    """The durable entry state a cancelled entry's OWN fills prove (§1).

    No filled leg walks to ``cleanup_required``, a partial fill to
    ``partial_entry``, and a complete target to ``entered`` - along the lifecycle
    edges that already exist (``created``/``entry_previewed`` -> ``entering`` ->
    the target). A run that has already left the entry states is left exactly as
    it is: this action never moves a run it did not strand.
    """
    from backend.options.execution.lifecycle import (
        mark_cleanup_required,
        mark_entered,
        mark_entering,
        mark_partial_entry,
    )
    from backend.options.execution.models import OptionRunStatus

    status = str(getattr(run, "status", "") or "")
    legs = [dict(leg or {}) for leg in (getattr(run, "legs", None) or [])]
    completed: List[str] = []
    failed: List[str] = []
    pending: List[str] = []
    for leg in legs:
        leg_id = str(leg.get("leg_id") or "")
        if not leg_id:
            continue
        target = int(leg.get("quantity") or 0)
        if str(leg.get("transaction_type") or "").upper() != "BUY":
            target = -target
        own = int(own_open.get(leg_id, 0) or 0)
        if own == 0:
            failed.append(leg_id)
        elif own == target:
            completed.append(leg_id)
        else:
            pending.append(leg_id)
    if completed and not pending and not failed:
        desired = OptionRunStatus.ENTERED.value
    elif not completed and not pending:
        desired = OptionRunStatus.CLEANUP_REQUIRED.value
    else:
        desired = OptionRunStatus.PARTIAL_ENTRY.value
    if status == desired:
        return run, False
    if status in (
        OptionRunStatus.CREATED.value,
        OptionRunStatus.ENTRY_PREVIEWED.value,
    ):
        stage = mark_entering(run)
    elif status == OptionRunStatus.ENTERING.value:
        stage = run
    else:
        stage = None
    if stage is not None:
        if desired == OptionRunStatus.ENTERED.value:
            return mark_entered(stage, completed_legs=completed), True
        if desired == OptionRunStatus.PARTIAL_ENTRY.value:
            return (
                mark_partial_entry(
                    stage,
                    completed_legs=completed,
                    failed_legs=failed,
                    pending_legs=pending,
                ),
                True,
            )
        # Every leg is flat: the run is stranded in cleanup (the case this action
        # exists for). The lifecycle has no single edge from ``entering`` there -
        # ``mark_partial_entry``'s all-failed shape lands on ``cleanup_required``,
        # which only a ``partial_entry`` run may enter - so walk the two edges it
        # does allow and write the final state. The run is never left claiming an
        # entry it does not hold.
        from backend.options.execution.lifecycle import transition_to

        stranded = stage
        if str(getattr(stranded, "status", "") or "") == OptionRunStatus.ENTERING.value:
            stranded = transition_to(stranded, OptionRunStatus.PARTIAL_ENTRY)
        return mark_cleanup_required(stranded), True
    if (
        status == OptionRunStatus.PARTIAL_ENTRY.value
        and desired == OptionRunStatus.CLEANUP_REQUIRED.value
    ):
        return mark_cleanup_required(run), True
    return run, False


def _durable_run_state(session: Any, option_run_id: str) -> Optional[Dict[str, Any]]:
    """A run's ``(status, orders)`` from the durable store, or ``None``."""
    try:
        row = session.execute(
            text(
                "SELECT status, orders FROM public.option_run_states "
                "WHERE strategy_run_id = :run"
            ),
            {"run": str(option_run_id)},
        ).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    orders = row.get("orders")
    if isinstance(orders, str):
        try:
            orders = json.loads(orders or "[]")
        except ValueError:
            orders = []
    return {"status": str(row.get("status") or ""), "orders": list(orders or [])}


async def _await_maybe(value: Any) -> Any:
    """Await a boundary call, allowing a plain-value fake in tests."""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


async def live_broker_cancel(*, account_id: str, order_id: str) -> Any:
    """The default live cancel boundary: the EXISTING orders service, by order id.

    Called only with a broker order id the platform already knows (the live
    claim's projection). A missing session, an unavailable broker or any
    transport error propagates, and the caller then records the cancel as
    UNVERIFIED rather than assuming it happened.
    """
    import asyncio

    from backend.api.routers.worker_shared import _load_live_kite_for_account
    from backend.broker_api.orders.service import OrdersService

    kite = await asyncio.to_thread(_load_live_kite_for_account, str(account_id))
    return await OrdersService().cancel_order(
        kite, "regular", str(order_id), f"owner-action-cancel-{order_id}"
    )


# ---------------------------------------------------------------------------
# flatten (B2.6b S3, section 3)
# ---------------------------------------------------------------------------


def _iso_at(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _job_stop_view(job: Any) -> Dict[str, Any]:
    """The stop evidence for ONE hosted job, and whether the stop is PROVEN.

    The same rule the operator stop route reports (``_stop_view``): a terminal
    label alone does not prove process cleanup, so a launched attempt stays
    unproven until the supervisor reports ``process_cleanup_state == 'confirmed'``.
    Flatten needs the boolean, so it is computed here once rather than read out of
    a response body.
    """
    status = str(getattr(job, "status", "") or "")
    launched = getattr(job, "handoff_at", None) is not None
    requested = (
        getattr(job, "stop_requested_at", None) is not None
        or str(getattr(job, "desired_state", "") or "") == "stopped"
    )
    cleanup = str(getattr(job, "process_cleanup_state", "") or "")
    if status in ACTIVE_JOB_STATUSES:
        proven = False
        state = "requested" if (status == "queued" or not launched) else "stopping"
    elif status in TERMINAL_JOB_STATUSES:
        proven = (cleanup == "confirmed") if launched else True
        state = "confirmed" if proven else "cleanup_unresolved"
    else:
        # An unknown status is never proof that the child stopped.
        proven = False
        state = "unknown"
    return {
        "job_id": str(getattr(job, "id", "") or ""),
        "attempt": int(getattr(job, "attempt", 0) or 0),
        "status": status,
        "desired_state": str(getattr(job, "desired_state", "") or ""),
        "requested": bool(requested),
        "state": state,
        "proven_stopped": bool(proven),
        "stop_requested_at": _iso_at(getattr(job, "stop_requested_at", None)),
        "stop_requested_by": getattr(job, "stop_requested_by", None),
        "handoff_at": _iso_at(getattr(job, "handoff_at", None)),
        "process_cleanup_state": cleanup or None,
    }


class FlattenOperationStore:
    """Durable, resumable flatten operations (``strategy_flatten_operations``).

    One OPEN operation per ``(strategy, account, environment)``: ``open()`` finds
    it or returns ``None``, and the schema's partial unique index is what makes a
    second concurrent POST fail rather than run a parallel flatten of one book.
    """

    #: Distinguishes "leave this field alone" from "set it to NULL" in ``save``.
    _UNSET = object()

    def __init__(self, *, session_factory: Any) -> None:
        self.session_factory = session_factory

    def open(self, scope: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        return self._one(scope, open_only=True)

    def latest(self, scope: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        return self._one(scope, open_only=False)

    def _one(
        self, scope: Mapping[str, Any], *, open_only: bool
    ) -> Optional[Dict[str, Any]]:
        stmt = select(StrategyFlattenOperation).where(
            StrategyFlattenOperation.account_id == str(scope["account_id"]),
            StrategyFlattenOperation.strategy_id == str(scope["strategy_id"]),
            StrategyFlattenOperation.execution_environment
            == str(scope["execution_environment"]),
        )
        if open_only:
            stmt = stmt.where(StrategyFlattenOperation.status != STATUS_COMPLETE)
        stmt = stmt.order_by(
            StrategyFlattenOperation.created_at.desc(),
            StrategyFlattenOperation.operation_id.desc(),
        ).limit(1)
        with self.session_factory() as session:
            row = session.execute(stmt).scalars().first()
        return None if row is None else _flatten_record(row)

    def create(
        self,
        scope: Mapping[str, Any],
        *,
        operation_id: str,
        actor: str,
        reason: str,
        stop: Mapping[str, Any],
        status: str,
    ) -> Dict[str, Any]:
        at = _utcnow()
        with self.session_factory() as session:
            row = StrategyFlattenOperation(
                operation_id=str(operation_id),
                strategy_id=str(scope["strategy_id"]),
                account_id=str(scope["account_id"]),
                execution_environment=str(scope["execution_environment"]),
                status=str(status),
                reason=str(reason or ""),
                actor_id=str(actor or ""),
                evidence_digest="",
                stop=dict(stop or {}),
                manifest={"items": []},
                created_at=at,
                updated_at=at,
            )
            session.add(row)
            session.commit()
            saved = _flatten_record(row)
        return saved

    def save(
        self,
        operation_id: str,
        *,
        status: Optional[str] = None,
        manifest: Optional[Mapping[str, Any]] = None,
        evidence_digest: Optional[str] = None,
        refusal: Any = _UNSET,
        stop: Optional[Mapping[str, Any]] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the operation's own fields; unsupplied fields are left alone."""
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyFlattenOperation).where(
                    StrategyFlattenOperation.operation_id == str(operation_id)
                )
            ).scalars().first()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "rejection_reason": "FLATTEN_OPERATION_NONE",
                        "operation_id": str(operation_id),
                    },
                )
            if status is not None:
                row.status = str(status)
            if manifest is not None:
                row.manifest = dict(manifest)
            if evidence_digest is not None:
                row.evidence_digest = str(evidence_digest)
            if refusal is not self._UNSET:
                row.refusal = None if refusal is None else str(refusal)
            if stop is not None:
                row.stop = dict(stop)
            if reason is not None:
                row.reason = str(reason)
            if actor is not None:
                row.actor_id = str(actor)
            row.updated_at = _utcnow()
            session.add(row)
            session.commit()
            saved = _flatten_record(row)
        return saved


def _flatten_record(row: Any) -> Dict[str, Any]:
    """One operation row as the manifest-bearing dict the service works with."""
    manifest = dict(getattr(row, "manifest", None) or {})
    return {
        "operation_id": str(getattr(row, "operation_id", "") or ""),
        "strategy_id": str(getattr(row, "strategy_id", "") or ""),
        "account_id": str(getattr(row, "account_id", "") or ""),
        "execution_environment": str(getattr(row, "execution_environment", "") or ""),
        "status": str(getattr(row, "status", "") or ""),
        "reason": str(getattr(row, "reason", "") or ""),
        "actor_id": str(getattr(row, "actor_id", "") or ""),
        "evidence_digest": str(getattr(row, "evidence_digest", "") or ""),
        "stop": dict(getattr(row, "stop", None) or {}),
        "refusal": getattr(row, "refusal", None),
        "items": [
            dict(item or {}) for item in (manifest.get("items") or []) if item
        ],
        "created_at": _iso_at(getattr(row, "created_at", None)),
        "updated_at": _iso_at(getattr(row, "updated_at", None)),
    }


def _flatten_item(
    kind: str,
    key: str,
    state: str,
    *,
    reason_code: Optional[str] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One manifest item: what flatten did (or still has to do) for one unit."""
    return {
        "kind": str(kind),
        "key": str(key),
        "state": str(state),
        "reason_code": None if reason_code in (None, "") else str(reason_code),
        "detail": dict(detail or {}),
    }


def _flatten_response(
    operation: Mapping[str, Any],
    *,
    done: Mapping[str, Any],
    missing: Sequence[str],
    audit_id: Optional[str],
) -> Dict[str, Any]:
    """The §5 body for a flatten POST / status GET.

    ``status`` is the operation's own verdict (never ``accepted``: this action
    either finished, is still working, or is blocked by name), ``missing`` is the
    subset of §3's done conditions that is not satisfied YET, and the ``stop``
    view is the evaluator evidence the whole operation was gated on.
    """
    operation_id = str(operation.get("operation_id") or "")
    return {
        "status": str(operation.get("status") or ""),
        # The operation IS the action here: it is the durable handle the owner
        # resumes, so it doubles as the action id.
        "action_id": operation_id,
        "operation_id": operation_id,
        "evidence_digest": str(operation.get("evidence_digest") or ""),
        "stop": dict(operation.get("stop") or {}),
        "items": [dict(row or {}) for row in (operation.get("items") or [])],
        "missing": [str(name) for name in (missing or [])],
        "done_conditions": {
            str(name): bool(dict(value or {}).get("satisfied"))
            for name, value in (done or {}).items()
        },
        "refusal": operation.get("refusal"),
        "audit_id": audit_id,
    }


def _merge_flatten_items(
    previous: Mapping[str, Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """The recomputed manifest: fresh items, plus work already DONE and merged.

    Two rules, both from §3 step 6. A key the fresh pass derived is the truth for
    that key (its evidence moved, so the new outcome is the one to report). A key
    that is only in the stored manifest and was ``done`` STAYS: it is completed
    work that no longer appears in the derived set (a closed book, an exited run),
    and forgetting it would make a resumed operation look like it never reduced
    anything.
    """
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for row in current:
        item = dict(row or {})
        key = str(item.get("key") or "")
        previous_row = dict(previous.get(key) or {})
        if (
            str(item.get("state")) == ITEM_STATE_BLOCKED
            and str(previous_row.get("state")) == ITEM_STATE_DONE
        ):
            # A later pass re-tried a key it had already completed and got a
            # refusal: the earlier terminal evidence outranks a retry's refusal
            # (the work WAS done; the new refusal is about what moved after).
            item = previous_row
        merged.append(item)
        seen.add(key)
    for key, row in previous.items():
        if key in seen or str(row.get("state")) != ITEM_STATE_DONE:
            continue
        merged.append(dict(row))
    merged.sort(key=lambda row: (str(row.get("kind") or ""), str(row.get("key") or "")))
    return merged


def _plan_legs(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    resolved = dict(plan.get("resolved_plan") or {})
    legs = resolved.get("legs")
    if not isinstance(legs, list):
        return []
    return [dict(leg) for leg in legs if isinstance(leg, Mapping)]


def plan_increases_exposure(plan: Mapping[str, Any], *, attributed_open: int) -> bool:
    """Whether this plan's OWN frozen target would increase the book's exposure.

    The tested rule is the executor's (``_opens_or_grows_exposure``: the book
    grows, or the trade crosses flat), so flatten and the paper executor cannot
    disagree about what "this reduces" means. An unreadable target is refused as
    an increase: the platform never admits a reduction it cannot prove reduces.
    """
    legs = _plan_legs(plan)
    if not legs:
        return True
    for leg in legs:
        target = _as_int(leg.get("signed_quantity"))
        if target is None:
            return True
        if _opens_or_grows_exposure(int(target), int(attributed_open)):
            return True
    return False


def target_zero_reduction_payload(book: Mapping[str, Any]) -> Dict[str, Any]:
    """The compiler payload for ONE ``(instrument, product)`` target-zero plan.

    Deliberately the executable single-instrument bundle: the frozen target is
    ZERO (a real instruction, not an absence), so the executor derives the order
    from the strategy's own attributed book and can only reduce. The payload is a
    pure function of the book, which is what makes a resumed operation recall the
    SAME frozen plan instead of freezing a new one.
    """
    return {
        "instrument_token": int(book.get("instrument_token") or 0),
        "exchange": str(book.get("exchange") or ""),
        "tradingsymbol": str(book.get("tradingsymbol") or ""),
        "product": str(book.get("product") or ""),
        "target_quantity": 0,
    }


def reduction_evaluation_id(
    operation_id: str, book: Mapping[str, Any]
) -> str:
    """The evaluation identity of ONE flatten reduction, stable across resumes."""
    instrument = str(book.get("instrument_id") or book.get("identity_key") or "")
    return f"flatten:{str(operation_id)}:{instrument}:{str(book.get('product') or '')}"


def latest_bound_run_id(
    session_factory: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
) -> Optional[str]:
    """The strategy's newest bound run in one environment, or ``None``.

    A reduction plan needs a run to attribute its fills to (the executor's own
    rule), and the strategy's own most recent attempt in this environment is the
    honest target - never a minted one.
    """
    with session_factory() as session:
        return session.execute(
            select(StrategyRunBinding.strategy_run_id)
            .where(
                StrategyRunBinding.account_id == str(account_id),
                StrategyRunBinding.strategy_id == str(strategy_id),
                StrategyRunBinding.execution_environment
                == str(execution_environment),
            )
            .order_by(
                StrategyRunBinding.bound_at.desc(),
                StrategyRunBinding.strategy_run_id.desc(),
            )
            .limit(1)
        ).scalars().first()


def default_reduction_planner(session_factory: Any) -> Any:
    """The governed planner for ONE attributed non-option book.

    The plan is created through the SAME proposal/compile path every other frozen
    plan uses (``ProposalStore.submit``), so it is validated, pinned and recorded
    before anything could admit it. The evaluation identity is derived from the
    operation and the book, so a resume recalls the existing frozen plan rather
    than freezing another one.
    """

    def build(
        scope: Mapping[str, Any],
        book: Mapping[str, Any],
        *,
        operation_id: str,
        actor: str,
    ) -> Dict[str, Any]:
        environment = str(scope["execution_environment"])
        run_id = latest_bound_run_id(
            session_factory,
            account_id=str(scope["account_id"]),
            strategy_id=str(scope["strategy_id"]),
            execution_environment=environment,
        )
        if not run_id:
            raise OwnerActionRefusal(
                FLATTEN_REDUCTION_RUN_UNBOUND,
                {
                    "strategy_id": str(scope["strategy_id"]),
                    "account_id": str(scope["account_id"]),
                    "execution_environment": environment,
                    "instrument_id": book.get("instrument_id"),
                    "product": book.get("product"),
                    "message": (
                        "no bound run exists to attribute this reduction to; a "
                        "flatten plan is never admitted without an attribution"
                    ),
                },
            )
        from backend.strategies.proposals import ProposalStore, ProposalSubmission

        store = ProposalStore(session_factory=session_factory)
        result = store.submit(
            ProposalSubmission(
                strategy_id=str(scope["strategy_id"]),
                account_id=str(scope["account_id"]),
                evaluation_id=reduction_evaluation_id(str(operation_id), book),
                evaluation_kind="run_now",
                strategy_run_id=str(run_id),
                target_kind="single_instrument",
                payload=target_zero_reduction_payload(book),
            )
        )
        plan_id = str((result.get("plan") or {}).get("plan_id") or "")
        if not plan_id:
            return {
                "plan": None,
                "reason_code": FLATTEN_REDUCTION_PLAN_REFUSED,
                "refusal": result.get("refusal")
                or {
                    "status": str(result.get("status") or ""),
                    "message": "the reduction plan was not validated",
                },
            }
        # Re-read the stored plan so the caller sees exactly what was frozen (the
        # same shape the admission and execute route work with).
        plan = store.get_plan(plan_id)
        if plan is None:
            return {
                "plan": None,
                "reason_code": FLATTEN_REDUCTION_PLAN_REFUSED,
                "refusal": {"plan_id": plan_id, "message": "frozen plan unreadable"},
            }
        return {
            "plan": plan,
            "plan_id": plan_id,
            "idempotent": bool(result.get("idempotent")),
            "actor": str(actor),
        }

    return build
