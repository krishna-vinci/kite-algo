"""Governed repair for option runs stranded in a partial / cleanup state (B2.1b).

B2.1a refuses every new option entry while this strategy owns a run that is not
provably finished (``OPTION_STRUCTURE_UNRESOLVED``). That refusal is correct, but
without a way out of ``partial_entry`` / ``partial_exit`` / ``cleanup_required``
the strategy is wedged forever. This module is that way out, and it is
deliberately a THIN reader of evidence that already exists rather than a second
execution engine:

* the run's OWN confirmed fills (``StagedStructureExit.reconcile_own_fills``
  records them; this module only READS them through the same adapter);
* the existing bounded exit rule (``StagedStructureExit.plan_exit`` ->
  ``build_structure_exit_orders``: short liabilities first, a hedge released only
  against proven short closure);
* the existing lifecycle transitions.

Two things are never done here. A run the platform cannot fully explain is never
auto-resolved: it is ``ambiguous`` and escalates by name, because a guess that
closes the wrong quantity is worse than a named blockage. And a residual close is
never an entry: every action is a close of a leg the run's own evidence says it
holds.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from .lifecycle import mark_cleanup_required, mark_closed, mark_exit_previewed, mark_exiting
from .models import OptionRunState, OptionRunStatus

#: Only a run the platform knows is unfinished-but-readable is repairable.
REPAIRABLE_RUN_STATUSES = (
    OptionRunStatus.PARTIAL_ENTRY.value,
    OptionRunStatus.PARTIAL_EXIT.value,
    OptionRunStatus.CLEANUP_REQUIRED.value,
)

STATE_FLAT = "flat"
STATE_RESIDUAL = "residual"
STATE_AMBIGUOUS = "ambiguous"
STATE_NOT_REPAIRABLE = "not_repairable"

REASON_NOT_REPAIRABLE = "OPTION_RUN_NOT_REPAIRABLE"
REASON_AMBIGUOUS = "OPTION_RUN_REPAIR_AMBIGUOUS"
REASON_EVIDENCE_CHANGED = "OPTION_RUN_REPAIR_EVIDENCE_CHANGED"
REASON_ACTION_MISMATCH = "OPTION_RUN_REPAIR_ACTION_MISMATCH"
REASON_STATE_CHANGED = "OPTION_RUN_REPAIR_STATE_CHANGED"
REASON_LIVE_UNSUPPORTED = "OPTION_RUN_REPAIR_LIVE_UNSUPPORTED"

ACTION_CLOSE_FLAT = "close_flat"
ACTION_CLOSE_RESIDUAL = "close_residual"
REPAIR_ACTIONS = (ACTION_CLOSE_FLAT, ACTION_CLOSE_RESIDUAL)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:32]


def _sorted_quantities(values: Any) -> Dict[str, int]:
    return {str(key): int(value) for key, value in sorted((values or {}).items())}


class OptionRunRepairRefusal(RuntimeError):
    """A named repair refusal. The caller maps it to an HTTP status + detail."""

    def __init__(
        self, reason_code: str, detail: Optional[Dict[str, Any]] = None, *, status_code: int = 409
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        self.status_code = int(status_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


def assess_option_run_repair(run: Any, staged_exit: Any) -> Dict[str, Any]:
    """Classify one option run from its OWN confirmed evidence. Side-effect free.

    Returns exactly one of ``flat`` / ``residual`` / ``ambiguous`` /
    ``not_repairable``, with a stable ``evidence_digest`` over the evidence the
    verdict rests on. Nothing is refreshed here: a GET must be able to report the
    state without moving it, and the POST re-derives the same digest before it is
    allowed to act.
    """
    option_run_id = str(getattr(run, "strategy_run_id", "") or "")
    status = str(getattr(run, "status", "") or "").strip().lower()
    legs = [dict(leg or {}) for leg in (getattr(run, "legs", []) or [])]
    known_leg_ids = {str(leg.get("leg_id") or "") for leg in legs}

    reasons: List[str] = []
    unreadable: List[Dict[str, Any]] = []
    unattributable: List[Dict[str, Any]] = []
    open_by_leg: Dict[str, int] = {}
    outstanding_buy: Dict[str, int] = {}
    outstanding_sell: Dict[str, int] = {}
    shorts_proven_closed = False
    is_flat = False
    unresolved: Optional[Dict[str, Any]] = None
    close_plan: List[Dict[str, Any]] = []
    close_detail: Dict[str, Any] = {}

    try:
        unresolved = staged_exit.unresolved_stage(run)
        open_by_leg = _sorted_quantities(staged_exit.own_open_by_leg(run))
        buy, sell = staged_exit.outstanding_by_symbol(run)
        outstanding_buy = _sorted_quantities(buy)
        outstanding_sell = _sorted_quantities(sell)
        shorts_proven_closed, _proven = staged_exit.short_closure_state(run)
        is_flat = bool(staged_exit.run_is_flat(run))
    except Exception as exc:  # noqa: BLE001 - an unreadable book is never "flat"
        unreadable.append({"stage": "own_fills", "error": type(exc).__name__})

    if not unreadable:
        # The run's OWN trades have to describe the run's OWN legs. A fill the run
        # cannot attribute is not evidence about it, and netting it (or ignoring it)
        # could prove a short closed that is still open.
        for trade in getattr(run, "trades", []) or []:
            row = dict(trade or {})
            leg_id = str(row.get("leg_id") or "")
            try:
                int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                unreadable.append(
                    {"stage": "trade", "leg_id": leg_id or None, "reason": "unreadable_quantity"}
                )
                continue
            if not leg_id or leg_id not in known_leg_ids:
                unattributable.append(
                    {
                        "leg_id": leg_id or None,
                        "tradingsymbol": row.get("tradingsymbol"),
                        "order_id": row.get("order_id"),
                    }
                )
        if unresolved is not None:
            reasons.append("protective_stage_unresolved")
        if outstanding_buy or outstanding_sell:
            reasons.append("orders_outstanding")
        if unattributable:
            reasons.append("unattributable_trades")
        if not reasons and not is_flat:
            try:
                orders, detail = staged_exit.plan_exit(run)
            except Exception as exc:  # noqa: BLE001 - no plan, no residual repair
                unreadable.append({"stage": "plan_exit", "error": type(exc).__name__})
            else:
                close_plan = [dict(order or {}) for order in (orders or [])]
                close_detail = dict(detail or {})
                if not close_plan:
                    # Open legs but no permitted bounded action: the platform will
                    # not invent one, and it will not call that "flat".
                    reasons.append("residual_close_unavailable")
    if unreadable:
        # An unreadable book is never "flat" and never a residual: the run is
        # escalated, not guessed at.
        reasons.append("unreadable_fills")

    if status not in REPAIRABLE_RUN_STATUSES:
        state = STATE_NOT_REPAIRABLE
        reason_code: Optional[str] = REASON_NOT_REPAIRABLE
        reasons = [f"status_{status or 'unknown'}"]
    elif reasons:
        state = STATE_AMBIGUOUS
        reason_code = REASON_AMBIGUOUS
    elif is_flat:
        state = STATE_FLAT
        reason_code = None
    else:
        state = STATE_RESIDUAL
        reason_code = None

    evidence = {
        "option_run_id": option_run_id,
        "status": status,
        "state": state,
        "open_by_leg": open_by_leg,
        "outstanding_buy": outstanding_buy,
        "outstanding_sell": outstanding_sell,
        "shorts_proven_closed": bool(shorts_proven_closed),
        "unresolved_stage": (
            None
            if unresolved is None
            else {
                "stage_digest": str(unresolved.get("stage_digest") or ""),
                "state": str(unresolved.get("state") or ""),
                "attempt": int(unresolved.get("attempt") or 1),
            }
        ),
        "close_plan": close_plan,
    }
    return {
        "option_run_id": option_run_id,
        "status": status,
        "state": state,
        "reason_code": reason_code,
        "reasons": reasons,
        "evidence_digest": _digest(evidence),
        "evidence": evidence,
        "close_plan": close_plan,
        "detail": close_detail,
        "unattributable_trades": unattributable,
        "unreadable_fills": unreadable,
    }


def _repair_exiting(run: OptionRunState, *, pending_legs: List[str]) -> OptionRunState:
    """The durable ``exiting`` state for a repaired run, along existing edges.

    The durable vocabulary has no direct edge from ``partial_entry`` /
    ``cleanup_required`` to ``exiting``; the repair walks the edges the lifecycle
    already allows (``partial_entry`` -> ``cleanup_required`` -> ``exit_previewed``
    -> ``exiting``) and only the FINAL state is ever persisted. That keeps one
    state machine for both the plan path and the repair path.
    """
    working = run
    if str(working.status) == OptionRunStatus.PARTIAL_ENTRY.value:
        working = mark_cleanup_required(working)
    if str(working.status) == OptionRunStatus.CLEANUP_REQUIRED.value:
        working = mark_exit_previewed(working)
    return mark_exiting(working, pending_legs=pending_legs)


def _repair_closed(run: OptionRunState) -> OptionRunState:
    """The durable ``exited`` state for a proven-flat run (existing edges only)."""
    return mark_closed(_repair_exiting(run, pending_legs=[]))


def pending_leg_ids(run: OptionRunState, close_plan: List[Dict[str, Any]]) -> List[str]:
    """The run leg ids a residual close is working on, in plan order."""
    by_symbol = {
        str((leg or {}).get("tradingsymbol") or ""): str((leg or {}).get("leg_id") or "")
        for leg in (getattr(run, "legs", []) or [])
    }
    pending: List[str] = []
    for order in close_plan or []:
        order = dict(order or {})
        leg_id = str(
            order.get("run_leg_id") or by_symbol.get(str(order.get("tradingsymbol") or "")) or ""
        )
        if leg_id and leg_id not in pending:
            pending.append(leg_id)
    return pending


class OptionRunRepairService:
    """Assess and apply a governed repair to ONE option run.

    The service owns no broker boundary and no second ledger: it reads the run
    through the durable store, derives the verdict from the run's own evidence
    via the existing staged-exit adapter, and moves the run with the SAME
    compare-and-set the execution path uses (``save_run_if_status``) - which is
    what makes the transition the per-run ownership token, so two repairs can
    never both act.
    """

    def __init__(self, *, run_store: Any, staged_exit: Any) -> None:
        self._run_store = run_store
        self._staged_exit = staged_exit

    def _run(self, option_run_id: str) -> OptionRunState:
        try:
            return self._run_store.get_run(str(option_run_id))
        except KeyError as exc:
            raise OptionRunRepairRefusal(
                "OPTION_RUN_NOT_FOUND", {"option_run_id": str(option_run_id)}, status_code=404
            ) from exc

    def assessment(self, option_run_id: str) -> Dict[str, Any]:
        """The read-only verdict for one run."""
        return assess_option_run_repair(self._run(option_run_id), self._staged_exit)

    def plan(
        self, *, option_run_id: str, action: str, evidence_digest: str
    ) -> Tuple[OptionRunState, Dict[str, Any]]:
        """Validate one action against the CURRENT evidence and name its next state.

        The digest the caller read must still describe the run: the platform
        never acts on evidence an operator saw before a fill moved it.
        """
        run = self._run(option_run_id)
        assessment = assess_option_run_repair(run, self._staged_exit)
        if str(evidence_digest or "") != str(assessment.get("evidence_digest") or ""):
            raise OptionRunRepairRefusal(
                REASON_EVIDENCE_CHANGED,
                {
                    "option_run_id": str(option_run_id),
                    "state": assessment.get("state"),
                    "message": "the run's own fill evidence changed; re-inspect before repairing",
                },
            )
        state = str(assessment.get("state") or "")
        if state == STATE_NOT_REPAIRABLE:
            raise OptionRunRepairRefusal(
                REASON_NOT_REPAIRABLE,
                {
                    "option_run_id": str(option_run_id),
                    "status": assessment.get("status"),
                    "message": "only a partial or cleanup-required run is repairable",
                },
            )
        if state == STATE_AMBIGUOUS:
            raise OptionRunRepairRefusal(
                REASON_AMBIGUOUS,
                {
                    "option_run_id": str(option_run_id),
                    "status": assessment.get("status"),
                    "reasons": list(assessment.get("reasons") or []),
                    "unattributable_trades": assessment.get("unattributable_trades"),
                    "unreadable_fills": assessment.get("unreadable_fills"),
                    "message": "this run cannot be explained from its own confirmed fills",
                },
            )
        if action == ACTION_CLOSE_FLAT:
            if state != STATE_FLAT:
                raise OptionRunRepairRefusal(
                    REASON_ACTION_MISMATCH,
                    {
                        "option_run_id": str(option_run_id),
                        "action": action,
                        "state": state,
                        "message": "close_flat is only permitted on a provably flat run",
                    },
                )
            return _repair_closed(run), assessment
        if action == ACTION_CLOSE_RESIDUAL:
            if state != STATE_RESIDUAL:
                raise OptionRunRepairRefusal(
                    REASON_ACTION_MISMATCH,
                    {
                        "option_run_id": str(option_run_id),
                        "action": action,
                        "state": state,
                        "message": "close_residual is only permitted on a run with a residual",
                    },
                )
            pending = pending_leg_ids(run, list(assessment.get("close_plan") or []))
            return _repair_exiting(run, pending_legs=pending), assessment
        raise OptionRunRepairRefusal(
            REASON_ACTION_MISMATCH,
            {"action": str(action), "supported": list(REPAIR_ACTIONS)},
            status_code=422,
        )

    def commit(self, next_run: OptionRunState, *, allowed_from: str) -> OptionRunState:
        """Take ownership of the run's transition, or refuse if another caller won."""
        won = self._run_store.save_run_if_status(next_run, allowed_from=(str(allowed_from),))
        if not won:
            raise OptionRunRepairRefusal(
                REASON_STATE_CHANGED,
                {
                    "option_run_id": str(next_run.strategy_run_id),
                    "observed_status": str(allowed_from),
                    "message": "another caller already moved this run; re-inspect before repairing",
                },
            )
        return next_run
