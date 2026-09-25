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
from dataclasses import replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .lifecycle import mark_cleanup_required, mark_closed, mark_exit_previewed, mark_exiting
from .models import OptionRunState, OptionRunStatus
from .plan_binding import PLAN_EXECUTION_FINISHED, PLAN_EXECUTION_UNKNOWN
from ..protection.staged_exit import known_run_legs

#: Only a run the platform knows is unfinished-but-readable is repairable.
#: ``adjusting`` joins them because a leg generation that stopped mid-flight is
#: exactly the stranded work this path exists for - but, unlike the others, it is
#: repairable only while the plan that owns the adjust has provably FINISHED
#: executing (``adjust_in_flight`` otherwise): the platform never closes a
#: structure out from under a plan that may still be submitting.
REPAIRABLE_RUN_STATUSES = (
    OptionRunStatus.PARTIAL_ENTRY.value,
    OptionRunStatus.PARTIAL_EXIT.value,
    OptionRunStatus.CLEANUP_REQUIRED.value,
    OptionRunStatus.ADJUSTING.value,
)

#: The statuses an owner-authorized discretionary exit (B2.6b S2) may act on.
#: An ``entered`` run is the whole point of the action - a structure the owner
#: wants OUT of - and a run already ``exiting`` / ``partial_exit`` is a
#: multi-stage exit that has not finished, so a later POST continues it. The
#: statuses BEFORE an entry are deliberately absent: an exit may not precede the
#: entry it closes.
OWNER_EXIT_RUN_STATUSES = (
    OptionRunStatus.PARTIAL_ENTRY.value,
    OptionRunStatus.CLEANUP_REQUIRED.value,
    OptionRunStatus.ADJUSTING.value,
    OptionRunStatus.ENTERED.value,
    OptionRunStatus.EXITING.value,
    OptionRunStatus.PARTIAL_EXIT.value,
)

#: A run that has not been entered yet cannot be exited. The plan-execution path
#: refuses these with the same name, so the owner exit does too.
BEFORE_ENTRY_RUN_STATUSES = (
    OptionRunStatus.CREATED.value,
    OptionRunStatus.ENTRY_PREVIEWED.value,
    OptionRunStatus.ENTERING.value,
)

#: A run already past the exit (the same vocabulary ``ownership`` releases an
#: owner row on). The owner exit reports it complete rather than exiting it again.
TERMINAL_RUN_STATUSES = (
    OptionRunStatus.EXITED.value,
    OptionRunStatus.SETTLED.value,
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

#: The owner exit's OWN refusal vocabulary (B2.6b §5). It is deliberately
#: distinct from ``OPTION_RUN_REPAIR_*``: the names below are what the Options UI
#: is built against, and a repair refusal means something narrower.
REASON_EXIT_ADJUST_IN_FLIGHT = "OPTION_RUN_ADJUST_IN_FLIGHT"
REASON_EXIT_PROTECTIVE_UNRESOLVED = "OPTION_PROTECTIVE_EXIT_UNRESOLVED"
REASON_EXIT_EVIDENCE_AMBIGUOUS = "OPTION_RUN_EVIDENCE_AMBIGUOUS"
REASON_EXIT_EVIDENCE_CHANGED = "OPTION_RUN_EXIT_EVIDENCE_CHANGED"
REASON_EXIT_STATE_CHANGED = "OPTION_RUN_STATE_CHANGED"
REASON_EXIT_NOT_APPLICABLE = "OPTION_RUN_EXIT_NOT_APPLICABLE"
REASON_EXIT_BEFORE_ENTRY = "OPTION_EXIT_BEFORE_ENTRY"

#: The assessment reason an ``adjusting`` run carries while the plan that owns
#: its adjust may still be submitting (or cannot be proven finished).
REASON_ADJUST_IN_FLIGHT = "adjust_in_flight"
REASON_LEDGER_INCOMPLETE = "ledger_incomplete"
#: The assessment reason an unresolved protective stage carries. Named here so
#: the owner exit can map it without restating the string.
REASON_PROTECTIVE_STAGE_UNRESOLVED = "protective_stage_unresolved"

ACTION_CLOSE_FLAT = "close_flat"
ACTION_CLOSE_RESIDUAL = "close_residual"
#: One owner-authorized discretionary exit stage (B2.6b S2).
ACTION_OWNER_EXIT = "owner_exit"
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


def assess_option_run_repair(
    run: Any,
    staged_exit: Any,
    *,
    adjust_owner: Optional[Mapping[str, Any]] = None,
    ledger_consistent: Optional[bool] = None,
    unresolved_step_reader: Any = None,
    owner_exit: bool = False,
) -> Dict[str, Any]:
    """Classify one option run from its OWN confirmed evidence. Side-effect free.

    Returns exactly one of ``flat`` / ``residual`` / ``ambiguous`` /
    ``not_repairable``, with a stable ``evidence_digest`` over the evidence the
    verdict rests on. Nothing is refreshed here: a GET must be able to report the
    state without moving it, and the POST re-derives the same digest before it is
    allowed to act.

    ``owner_exit`` widens WHICH statuses the same verdict may describe: the
    owner-authorized discretionary exit (B2.6b S2) acts on an ``entered`` run -
    and continues one already ``exiting`` - so those join the repairable set and
    a run already past the exit is reported ``flat`` (complete) instead of
    ``not_repairable``. Every rule about the EVIDENCE is unchanged, because the
    two callers must not disagree about what the run holds.

    ``adjust_owner`` is the execution state of the plan(s) that own an
    ``adjusting`` run's adjust phase (``option_adjust_owner_state``). A caller
    that cannot supply it, or supplies anything but ``finished``, cannot be told
    that the run is stranded: the verdict is ``ambiguous`` with the reason
    ``adjust_in_flight`` rather than a close the platform might race.

    A ROLLED run is a run the platform can still explain: the roll re-keys
    ``run.legs`` to the new generation, and the released generations stay in the
    run's own bounded metadata history. A fill on a released leg id is therefore
    the run's OWN evidence (``StagedStructureExit.known_run_legs`` is the one rule
    for which legs those are). A released leg that nets to ZERO is gone; one that
    nets NON-ZERO is residual exposure and joins the close plan. A leg id in
    neither the held legs nor any recorded generation stays unattributable, and
    an UNREADABLE history is ``ambiguous``: the platform will not read a book it
    cannot reconstruct, and it will not guess a leg away.

    ``unresolved_step_reader`` names the step(s) this verdict is waiting on, so an
    ``ambiguous`` run is addressable without a second opinion about what
    "unresolved" means: the WHICH comes from the fold's own evidence
    (:func:`unresolved_step_coordinates`), and the reader only supplies the step's
    own newest trail word and linked order id.
    """
    option_run_id = str(getattr(run, "strategy_run_id", "") or "")
    status = str(getattr(run, "status", "") or "").strip().lower()
    # The run's own legs INCLUDING the generations a roll released and recorded
    # in its metadata (``StagedStructureExit.known_run_legs``). After a roll the
    # held legs are the new generation, but the OLD generation's confirmed fills
    # still carry its leg ids - and those ids are still THIS run's evidence, not
    # a foreign fill. Reading the rule from the same helper the exit engine uses
    # is what keeps the repair/owner-exit book and the protection book one.
    legs: List[Dict[str, Any]] = []
    known_leg_ids: set = set()

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
        legs = [dict(leg or {}) for leg in staged_exit.known_run_legs(run)]
        known_leg_ids = {str(leg.get("leg_id") or "") for leg in legs}
    except Exception as exc:  # noqa: BLE001 - an unreadable history is not "flat"
        unreadable.append({"stage": "leg_history", "error": type(exc).__name__})

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
        # The run's OWN trades have to describe the run's OWN legs - the held
        # generation OR a generation a roll recorded as released. A fill in
        # NEITHER is not evidence about this run, and netting it (or ignoring it)
        # could prove a short closed that is still open; an unreadable history is
        # the same refusal, because it may be hiding the leg that explains a fill.
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
            reasons.append(REASON_PROTECTIVE_STAGE_UNRESOLVED)
        if (outstanding_buy or outstanding_sell) and not owner_exit:
            # The owner exit reads the SAME outstanding quantity the staged exit
            # engine nets by, and those orders come from the run's OWN stage
            # records: a working stage is the exit WAITING, not evidence the
            # platform cannot explain. The repair path keeps the stricter reading
            # because an operator repair must not race work it did not submit.
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
                if not close_plan and not owner_exit:
                    # Open legs but no permitted bounded action: the platform will
                    # not invent one, and it will not call that "flat".
                    # Owner exit: nothing is releasable RIGHT NOW (a short still
                    # owes its proof), which is waiting, not ambiguity.
                    reasons.append("residual_close_unavailable")
    if unreadable:
        # An unreadable book is never "flat" and never a residual: the run is
        # escalated, not guessed at.
        reasons.append("unreadable_fills")
    if ledger_consistent is False:
        reasons.append(REASON_LEDGER_INCOMPLETE)

    # Why a hedge the run still holds is NOT released yet. The exit builder omits
    # a hedge from its plan entirely until its short is PROVEN closed (there is no
    # permitted action to report), so the reason has to be named from the run's own
    # evidence - §7: proof-based waiting is expected, and the owner has to be able
    # to see what it is waiting for.
    withheld_hedges: List[Dict[str, Any]] = []
    if (
        not is_flat
        and not shorts_proven_closed
        and not unreadable
        and not unattributable
    ):
        for leg in legs:
            if str(leg.get("transaction_type") or "").upper() != "BUY":
                continue
            leg_id = str(leg.get("leg_id") or "")
            open_quantity = max(0, int(open_by_leg.get(leg_id, 0) or 0))
            if open_quantity <= 0:
                continue
            withheld_hedges.append(
                {
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "quantity": open_quantity,
                    "reason": "short_not_proven_closed",
                }
            )

    # An ``adjusting`` run is mid-mutation: it is only repairable once the plan
    # that owns its adjust has provably finished executing. While that plan may
    # still be submitting - or while the platform cannot prove it is not - the
    # verdict is ``ambiguous``, never a close that races it.
    owner_state = dict(adjust_owner or {})
    if status == OptionRunStatus.ADJUSTING.value and (
        str(owner_state.get("state") or "") != PLAN_EXECUTION_FINISHED
    ):
        reasons.append(REASON_ADJUST_IN_FLIGHT)

    reason_code: Optional[str]
    admissible = OWNER_EXIT_RUN_STATUSES if owner_exit else REPAIRABLE_RUN_STATUSES
    if owner_exit and status in TERMINAL_RUN_STATUSES:
        # Past the exit already: the owner exit reports the run's own evidence -
        # ``flat`` when it finished, ``ambiguous`` when the terminal status
        # disagrees with the fills - and never exits it a second time.
        if reasons or not is_flat:
            state = STATE_AMBIGUOUS
            reason_code = REASON_AMBIGUOUS
            if not reasons:
                reasons = ["terminal_run_not_flat"]
        else:
            state = STATE_FLAT
            reason_code = None
    elif status not in admissible:
        state = STATE_NOT_REPAIRABLE
        reason_code = REASON_NOT_REPAIRABLE
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
        "adjust_owner": owner_state or None,
        "open_by_leg": open_by_leg,
        "outstanding_buy": outstanding_buy,
        "outstanding_sell": outstanding_sell,
        "shorts_proven_closed": bool(shorts_proven_closed),
        "withheld_hedges": withheld_hedges,
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
        "withheld_hedges": (
            withheld_hedges
            if withheld_hedges
            else [dict(row or {}) for row in (close_detail.get("withheld_hedges") or [])]
        ),
        "unattributable_trades": unattributable,
        "unreadable_fills": unreadable,
        "unresolved_steps": unresolved_step_coordinates(
            owner_state, step_detail_reader=unresolved_step_reader
        ),
    }


def unresolved_step_coordinates(
    adjust_owner: Optional[Mapping[str, Any]],
    *,
    step_detail_reader: Any = None,
) -> List[Dict[str, Any]]:
    """The adjust owner plans' unresolved steps, with their trail coordinates.

    The WHICH is the shared fold's answer, read through
    ``option_adjust_owner_state``'s per-plan evidence
    (``plan_binding.option_plan_execution_state``): only a plan whose own
    execution is ``in_flight`` names unresolved steps, and a step the fold cannot
    explain is never invented here. ``state`` / ``order_id`` come from the step's
    OWN newest trail row through ``step_detail_reader`` - a plain read of the row
    the fold already reads, not a second verdict about whether the step is
    unresolved. Without a reader they are ``""`` / ``None`` rather than guessed.
    """
    plans = dict((adjust_owner or {}).get("plans") or {})
    out: List[Dict[str, Any]] = []
    for plan_id in sorted(str(value) for value in plans):
        entry = dict(plans.get(plan_id) or {})
        evidence = dict(entry.get("evidence") or {})
        step_numbers = {
            int(value) for value in (evidence.get("unresolved_steps") or [])
        }
        for step_no in sorted(step_numbers):
            detail: Dict[str, Any] = {}
            if step_detail_reader is not None:
                try:
                    detail = dict(step_detail_reader(str(plan_id), int(step_no)) or {})
                except Exception:  # noqa: BLE001 - an unreadable row is not a state
                    detail = {}
            order_id = detail.get("order_id")
            out.append(
                {
                    "plan_id": str(plan_id),
                    "step_no": int(step_no),
                    "state": str(detail.get("state") or ""),
                    "order_id": None if order_id in (None, "") else str(order_id),
                }
            )
    return out


def _repair_exiting(run: OptionRunState, *, pending_legs: List[str]) -> OptionRunState:
    """The durable ``exiting`` state for a repaired run, along existing edges.

    The durable vocabulary has no direct edge from ``partial_entry`` /
    ``adjusting`` / ``cleanup_required`` (nor from ``entered``) to ``exiting``;
    both repair and the owner exit walk the edges the lifecycle already allows
    (``partial_entry`` | ``adjusting`` -> ``cleanup_required``,
    ``entered`` | ``cleanup_required`` -> ``exit_previewed`` -> ``exiting``) and
    only the FINAL state is ever persisted. A run already ``exiting`` stays
    there: it only takes the current stage's legs. That keeps one state machine
    for the plan path, the repair path and the owner exit alike.
    """
    working = run
    if str(working.status) in (
        OptionRunStatus.PARTIAL_ENTRY.value,
        OptionRunStatus.ADJUSTING.value,
    ):
        working = mark_cleanup_required(working)
    if str(working.status) in (
        OptionRunStatus.CLEANUP_REQUIRED.value,
        OptionRunStatus.ENTERED.value,
    ):
        working = mark_exit_previewed(working)
    if str(working.status) == OptionRunStatus.EXITING.value:
        # Already exiting: the status carries, only the stage's legs change.
        return replace(working, pending_legs=list(pending_legs))
    return mark_exiting(working, pending_legs=pending_legs)


def _repair_closed(run: OptionRunState) -> OptionRunState:
    """The durable ``exited`` state for a proven-flat run (existing edges only)."""
    return mark_closed(_repair_exiting(run, pending_legs=[]))


def _owner_exit_closed(run: OptionRunState) -> OptionRunState:
    """The durable ``exited`` state for a run whose own fills prove it flat.

    A run already past the exit is returned unchanged - the owner exit reports
    it complete instead of writing the same terminal status again.
    """
    if str(run.status) in TERMINAL_RUN_STATUSES:
        return run
    return mark_closed(_repair_exiting(run, pending_legs=[]))


def option_adjust_owner_reader(session_factory: Any) -> Callable[[str], Dict[str, Any]]:
    """The SHARED takeover rule as a reader the repair service can use.

    One rule, two callers: the adjust gate and the repair assessment must agree
    about whether the plan that owns an ``adjusting`` run may still be submitting,
    so the repair path reads exactly what ``assess_option_adjust_admissibility``
    reads rather than re-deriving it.
    """

    def _read(option_run_id: str) -> Dict[str, Any]:
        from .plan_binding import option_adjust_owner_state

        with session_factory() as session:
            return dict(option_adjust_owner_state(str(option_run_id), session=session))

    return _read


def option_run_ledger_consistency_reader(session_factory: Any) -> Callable[[Any], bool]:
    """The shared trail-vs-ledger rule, read through every bound plan edge."""

    def _read(run: Any) -> bool:
        from .plan_binding import (
            option_run_bound_plan_ids,
            option_run_ledger_consistent,
        )

        with session_factory() as session:
            plan_ids = option_run_bound_plan_ids(
                str(getattr(run, "strategy_run_id", "") or ""), session=session
            )
            return option_run_ledger_consistent(run, plan_ids, session=session)

    return _read


def pending_leg_ids(run: OptionRunState, close_plan: List[Dict[str, Any]]) -> List[str]:
    """The run leg ids a residual close is working on, in plan order."""
    # Resolved against the SAME known legs the close plan was derived from - the
    # held generation and any generation a roll released - so a residual held by
    # an old leg is recorded as that leg's pending close, not as none.
    by_symbol: Dict[str, str] = {}
    for leg in known_run_legs(run):
        leg = dict(leg or {})
        symbol = str(leg.get("tradingsymbol") or "")
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = str(leg.get("leg_id") or "")
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

    ``adjust_owner_reader`` is what lets an ``adjusting`` run be classified: the
    owner of its adjust phase is read through the SHARED takeover rule
    (:func:`option_adjust_owner_reader`). Without it, an ``adjusting`` verdict is
    ``ambiguous`` by name rather than assumed stranded.
    """

    def __init__(
        self,
        *,
        run_store: Any,
        staged_exit: Any,
        adjust_owner_reader: Any = None,
        ledger_consistency_reader: Any = None,
        unresolved_step_reader: Any = None,
    ) -> None:
        self._run_store = run_store
        self._staged_exit = staged_exit
        self._adjust_owner_reader = adjust_owner_reader
        self._ledger_consistency_reader = ledger_consistency_reader
        #: Names the coordinates of the steps an ``adjust_in_flight`` verdict is
        #: waiting on. Absent, the assessment still reports the refusal; it just
        #: cannot say which plan/step it means.
        self._unresolved_step_reader = unresolved_step_reader

    def _ledger_consistent(self, run: Any) -> Optional[bool]:
        if self._ledger_consistency_reader is None:
            return None
        try:
            return bool(self._ledger_consistency_reader(run))
        except Exception:
            return False

    def _adjust_owner(self, option_run_id: str) -> Optional[Dict[str, Any]]:
        """The execution state of the plan(s) that own this run's adjust phase.

        The reader is the SHARED takeover rule
        (:func:`option_adjust_owner_reader`). A reader that is missing, or that
        fails, yields no verdict at all: an ``adjusting`` run is then ``ambiguous``
        by name rather than assumed stranded.
        """
        if self._adjust_owner_reader is None:
            return None
        try:
            return dict(self._adjust_owner_reader(str(option_run_id)) or {})
        except Exception as exc:  # noqa: BLE001 - an unreadable owner is never "finished"
            return {
                "state": PLAN_EXECUTION_UNKNOWN,
                "reason": "adjust_owner_read_failed",
                "error": type(exc).__name__,
            }

    def _run(self, option_run_id: str) -> OptionRunState:
        try:
            return self._run_store.get_run(str(option_run_id))
        except KeyError as exc:
            raise OptionRunRepairRefusal(
                "OPTION_RUN_NOT_FOUND", {"option_run_id": str(option_run_id)}, status_code=404
            ) from exc

    def run(self, option_run_id: str) -> OptionRunState:
        """The durable run itself, for a caller that needs its own metadata."""
        return self._run(option_run_id)

    def _owner_exit_evidence_run(self, run: OptionRunState) -> OptionRunState:
        """The run a submission would actually derive from, without moving it.

        A submission translates the confirmed fills of the run's OWN exit orders
        (``StagedStructureExit.reconcile_own_fills``) onto the run BEFORE it
        derives, so an assessment that read only the persisted trades would
        describe a structure the submission never trades - a close plan the exit
        would not send, and a digest pinned to it. The translation is read-only
        here, so a GET still moves nothing.
        """
        translate = getattr(self._staged_exit, "translate_own_fills", None)
        if translate is None:
            return run
        try:
            translated = dict(translate(run) or {})
        except Exception:  # noqa: BLE001 - an unreadable translation is not evidence
            return run
        recorded = [
            dict(row or {}) for row in (translated.get("recorded") or []) if row
        ]
        if not recorded:
            return run
        already = {
            str(dict(trade or {}).get("stage_fill_id") or "")
            for trade in (getattr(run, "trades", None) or [])
        }
        fresh = [
            row
            for row in recorded
            if str(row.get("stage_fill_id") or "") not in already
        ]
        if not fresh:
            return run
        return replace(run, trades=list(run.trades) + fresh)

    def assessment(self, option_run_id: str, *, owner_exit: bool = False) -> Dict[str, Any]:
        """The read-only verdict for one run.

        ``owner_exit`` asks the same question of the same evidence through the
        owner-exit status set (an ``entered`` run is admissible, a run already
        past the exit reads ``flat``).
        """
        run = self._run(option_run_id)
        if owner_exit:
            run = self._owner_exit_evidence_run(run)
        return assess_option_run_repair(
            run,
            self._staged_exit,
            adjust_owner=self._adjust_owner(option_run_id),
            ledger_consistent=self._ledger_consistent(run),
            unresolved_step_reader=self._unresolved_step_reader,
            owner_exit=owner_exit,
        )

    def plan(
        self,
        *,
        option_run_id: str,
        action: str,
        evidence_digest: str,
        owner_exit: bool = False,
    ) -> Tuple[OptionRunState, Dict[str, Any]]:
        """Validate one action against the CURRENT evidence and name its next state.

        The digest the caller read must still describe the run: the platform
        never acts on evidence an operator saw before a fill moved it.

        ``owner_exit`` runs the same validation in the owner-exit view: the
        admissible statuses are wider (``entered`` and a run already ``exiting``)
        and ``ACTION_OWNER_EXIT`` names the transition. The evidence rules - the
        digest, the ambiguity verdict, the CAS - are untouched.
        """
        run = self._run(option_run_id)
        assessment_run = self._owner_exit_evidence_run(run) if owner_exit else run
        assessment = assess_option_run_repair(
            assessment_run,
            self._staged_exit,
            adjust_owner=self._adjust_owner(option_run_id),
            ledger_consistent=self._ledger_consistent(run),
            unresolved_step_reader=self._unresolved_step_reader,
            owner_exit=owner_exit,
        )
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
        if action == ACTION_OWNER_EXIT:
            # The discretionary exit of ONE structure: a flat run is COMPLETE
            # (its own fills prove nothing is held), otherwise the run takes the
            # ``exiting`` state and the close plan becomes the stage's pending
            # legs. The caller submits ONE stage of that plan and never a
            # freshly compiled order list.
            if state == STATE_FLAT:
                return _owner_exit_closed(run), assessment
            if state == STATE_RESIDUAL:
                pending = pending_leg_ids(run, list(assessment.get("close_plan") or []))
                return _repair_exiting(run, pending_legs=pending), assessment
            raise OptionRunRepairRefusal(
                REASON_ACTION_MISMATCH,
                {
                    "option_run_id": str(option_run_id),
                    "action": action,
                    "state": state,
                    "message": "an owner exit needs a run that holds or is already exiting",
                },
            )
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
