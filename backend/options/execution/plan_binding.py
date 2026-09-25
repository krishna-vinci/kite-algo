"""Durable edge: frozen ``option_structure`` plan -> existing option run.

The options lane already has a durable run engine (``option_run_states`` +
``DurableOptionRunStore`` + ``lifecycle``). What it did not have was a durable
way for a *paper plan execution* to say which run it created (entry) or which
run it closes (exit) without overloading the hosted worker-run id as the
option-run id. This module is that edge and nothing else:

- :class:`PlanOptionRunBindingStore` persists/reads the relation.
- :func:`resolve_plan_option_run` turns one frozen plan into the run it should
  execute against, creating the run for an entry plan and *validating* (never
  trusting) the caller's reference for an exit plan.

The caller-supplied option-run reference is a lookup key, not authority: the
canonical strategy/account/environment and the frozen leg identities are
re-checked against what the platform persisted.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import text
from backend.app.database import SessionLocal
from backend.options.protection.ownership import (
    OptionProtectionOwnerStore,
    option_protection_policy_version,
    option_protection_policy_snapshot,
)

from .durable_store import DurableOptionRunStore
from .models import OptionRunCreateRequest, OptionRunState


class PlanBindingRefusal(Exception):
    """A named refusal carrying the executor's reason code and detail."""

    def __init__(self, reason_code: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})

    def as_detail(self) -> dict[str, Any]:
        payload = {"reason_code": self.reason_code}
        payload.update(self.detail)
        return payload


class PlanBindingConflict(PlanBindingRefusal):
    """The plan already resolves to a binding with a different identity."""


_BINDING_COLUMNS = (
    "plan_id",
    "option_run_id",
    "worker_run_id",
    "strategy_id",
    "account_id",
    "execution_environment",
    "phase",
)

#: A run in one of these states is FINISHED: it no longer holds (and can no
#: longer move) the structure, so it is not a duplicate of a new entry.
_TERMINAL_RUN_STATUSES = frozenset({"exited", "settled"})

#: A run in one of these states still owns work the platform has neither proved
#: finished nor repaired, so opening ANY new option structure on top of it would
#: stack exposure the operator never approved. ``entered`` is deliberately
#: absent: a cleanly held DIFFERENT structure never blocks a new entry.
_UNRESOLVED_RUN_STATUSES = frozenset(
    {"created", "entry_previewed", "entering", "partial_entry",
     "cleanup_required", "exit_previewed", "exiting", "partial_exit",
     # An adjust that has not landed is in-flight work like any other: the run's
     # leg generation is moving, so a new ENTRY on top of it would stack exposure
     # the operator never approved (B2.1a's rule, unchanged).
     "adjusting"}
)

#: The durable vocabulary of ``backend.options.execution.models.OptionRunStatus``.
#: A status outside it is UNKNOWN, which is never treated as "finished".
_KNOWN_RUN_STATUSES = _UNRESOLVED_RUN_STATUSES | {"entered"} | _TERMINAL_RUN_STATUSES


class PlanOptionRunBindingStore:
    """Insert-only binding store. A plan binds once; a retry returns the row."""

    def __init__(self, *, session_factory: Callable[[], Any] = SessionLocal) -> None:
        self._session_factory = session_factory
        #: Public alias: the atomic entry path opens the shared transaction.
        self.session_factory = session_factory

    @staticmethod
    def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "plan_id": str(row.get("plan_id")),
            "option_run_id": str(row.get("option_run_id")),
            "worker_run_id": None if row.get("worker_run_id") is None else str(row.get("worker_run_id")),
            "strategy_id": str(row.get("strategy_id")),
            "account_id": str(row.get("account_id")),
            "execution_environment": str(row.get("execution_environment")),
            "phase": str(row.get("phase")),
        }

    def get(self, plan_id: str, *, db: Any = None) -> Optional[dict[str, Any]]:
        if not plan_id:
            return None
        owns_session = db is None
        session = db or self._session_factory()
        try:
            row = (
                session.execute(
                    text(
                        """
                        SELECT plan_id, option_run_id, worker_run_id, strategy_id,
                               account_id, execution_environment, phase
                        FROM public.strategy_plan_option_runs
                        WHERE plan_id = :plan_id
                        """
                    ),
                    {"plan_id": str(plan_id)},
                )
                .mappings()
                .first()
            )
        finally:
            if owns_session:
                session.close()
        return None if row is None else self._row_to_dict(dict(row))

    def list_for_run(self, option_run_id: str) -> list[dict[str, Any]]:
        if not option_run_id:
            return []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    text(
                        """
                        SELECT plan_id, option_run_id, worker_run_id, strategy_id,
                               account_id, execution_environment, phase
                        FROM public.strategy_plan_option_runs
                        WHERE option_run_id = :option_run_id
                        ORDER BY created_at, plan_id
                        """
                    ),
                    {"option_run_id": str(option_run_id)},
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(dict(row)) for row in rows]

    def bind(
        self,
        *,
        plan_id: str,
        option_run_id: str,
        strategy_id: str,
        account_id: str,
        execution_environment: str,
        phase: str,
        worker_run_id: Optional[str] = None,
        db: Any = None,
    ) -> dict[str, Any]:
        """Idempotently bind ``plan_id`` to ``option_run_id``.

        A retry with the SAME identity returns the stored row (never a second
        run). A retry that would change the run, phase or scope is a conflict and
        refuses, because silently rebinding a plan is how one plan would execute
        against two runs.
        """
        plan_id = str(plan_id or "")
        option_run_id = str(option_run_id or "")
        phase = str(phase or "")
        if not plan_id or not option_run_id:
            raise PlanBindingRefusal(
                "OPTION_PLAN_BINDING_INVALID",
                {"plan_id": plan_id, "option_run_id": option_run_id},
            )
        if phase not in ("entry", "exit", "adjust"):
            raise PlanBindingRefusal(
                "OPTION_PLAN_BINDING_INVALID",
                {"plan_id": plan_id, "phase": phase},
            )
        wanted = {
            "plan_id": plan_id,
            "option_run_id": option_run_id,
            "worker_run_id": None if worker_run_id is None else str(worker_run_id),
            "strategy_id": str(strategy_id),
            "account_id": str(account_id),
            "execution_environment": str(execution_environment),
            "phase": phase,
        }
        owns_session = db is None
        session = db or self._session_factory()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO public.strategy_plan_option_runs (
                        plan_id, option_run_id, worker_run_id, strategy_id,
                        account_id, execution_environment, phase
                    ) VALUES (
                        :plan_id, :option_run_id, :worker_run_id, :strategy_id,
                        :account_id, :execution_environment, :phase
                    )
                    ON CONFLICT (plan_id) DO NOTHING
                    """
                ),
                wanted,
            )
            # Re-read INSIDE the caller's transaction: a second session on a
            # pooled connection would either miss the uncommitted row (real DB)
            # or roll the caller's transaction back (single-connection fixtures).
            stored = self.get(plan_id, db=session)
            if owns_session:
                session.commit()
        finally:
            if owns_session:
                session.close()
        if stored is None:
            raise PlanBindingRefusal(
                "OPTION_PLAN_BINDING_MISSING",
                {"plan_id": plan_id},
            )
        mismatched = {
            key: {"stored": stored.get(key), "requested": wanted.get(key)}
            for key in ("option_run_id", "strategy_id", "account_id", "execution_environment", "phase")
            if stored.get(key) != wanted.get(key)
        }
        # ``worker_run_id`` is informational attribution, not identity: a plan can
        # be re-driven by a new worker run and must not refuse on that alone.
        if mismatched:
            raise PlanBindingConflict(
                "OPTION_PLAN_BINDING_CONFLICT",
                {"plan_id": plan_id, "mismatched": mismatched},
            )
        return stored


def _leg_identity(leg: Mapping[str, Any]) -> str:
    value = leg.get("instrument_id")
    if value:
        return str(value)
    return str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "").strip().upper()


def _run_leg_identity(leg: Mapping[str, Any]) -> str:
    metadata = leg.get("metadata") or {}
    value = metadata.get("instrument_id") if isinstance(metadata, Mapping) else None
    if value:
        return str(value)
    return str(leg.get("tradingsymbol") or "").strip().upper()


def _frozen_option_run_block(plan: Mapping[str, Any]) -> dict[str, Any]:
    resolved = plan.get("resolved_plan") or {}
    block = resolved.get("option_run")
    return dict(block) if isinstance(block, Mapping) else {}


def _frozen_phase(plan: Mapping[str, Any], *, default: str) -> str:
    block = _frozen_option_run_block(plan)
    phase = str(block.get("phase") or "").strip().lower()
    if phase in ("entry", "exit", "adjust"):
        return phase
    if phase:
        raise PlanBindingRefusal(
            "OPTION_PLAN_PHASE_INVALID",
            {"plan_id": str(plan.get("plan_id") or ""), "phase": phase},
        )
    return default


def _frozen_structure_digest(plan: Mapping[str, Any]) -> str:
    resolved = plan.get("resolved_plan") or {}
    return str(resolved.get("structure_digest") or "")


def _plan_leg_keys(legs: Any) -> list[tuple[str, str]]:
    """The frozen legs of a plan as ``(identity, side)`` pairs.

    Position is deliberately NOT part of the identity: a structure is a set of
    legs, and a re-frozen plan that lists the same legs in another order is the
    same structure.
    """
    keys: list[tuple[str, str]] = []
    for leg in list(legs or []):
        if not isinstance(leg, Mapping):
            continue
        identity = _leg_identity(leg)
        if not identity:
            continue
        keys.append((identity, str(leg.get("side") or "").strip().upper()))
    return sorted(keys)


def _run_leg_keys(legs: Any) -> list[tuple[str, str]]:
    """The durable run's legs as the same ``(identity, side)`` pairs."""
    keys: list[tuple[str, str]] = []
    for leg in list(legs or []):
        if not isinstance(leg, Mapping):
            continue
        identity = _run_leg_identity(leg)
        if not identity:
            continue
        keys.append((identity, str(leg.get("transaction_type") or "").strip().upper()))
    return sorted(keys)


def _same_structure(
    plan: Mapping[str, Any], run_row: Mapping[str, Any]
) -> Optional[bool]:
    """Whether a discovered option run IS the structure this plan would open.

    ``True``/``False``/``None`` - and ``None`` (unknown) is a refusal at the
    caller, never "different". The frozen ``structure_digest`` decides it when
    both sides carry one; otherwise the leg identity set decides it, and a side
    whose legs cannot be read leaves the question open.
    """
    plan_digest = _frozen_structure_digest(plan)
    run_digest = str(run_row.get("structure_digest") or "")
    if plan_digest and run_digest:
        return plan_digest == run_digest
    plan_keys = _plan_leg_keys(_entry_legs(plan))
    run_keys = _run_leg_keys(run_row.get("legs"))
    if not plan_keys or not run_keys:
        return None
    return plan_keys == run_keys


def is_option_entry_plan(plan: Mapping[str, Any]) -> bool:
    """Whether the option-entry gate applies to this frozen plan.

    The gate covers exactly an ``option_structure`` plan whose frozen phase is
    ``entry``. An exit plan closes work that already exists, and every other plan
    kind has no option run to duplicate, so both are left untouched. The phase is
    read from the FROZEN block only: re-deciding it here would be a second copy
    of the compiler's contract.
    """
    resolved = plan.get("resolved_plan") or {}
    if str(resolved.get("target_kind") or "") != "option_structure":
        return False
    block = resolved.get("option_run")
    declared = block.get("phase") if isinstance(block, Mapping) else None
    return str(declared or "entry").strip().lower() == "entry"


def is_option_adjust_plan(plan: Mapping[str, Any]) -> bool:
    """Whether the option-ADJUST gate applies to this frozen plan.

    Exactly an ``option_structure`` plan whose frozen phase is ``adjust`` - the
    mutation counterpart of :func:`is_option_entry_plan`, read from the FROZEN
    block for the same reason: re-deciding the phase here would be a second copy
    of the compiler's contract. An entry plan opens work (its own gate) and an
    exit plan closes it, so neither is gated by the mutation rules.
    """
    resolved = plan.get("resolved_plan") or {}
    if str(resolved.get("target_kind") or "") != "option_structure":
        return False
    block = resolved.get("option_run")
    declared = block.get("phase") if isinstance(block, Mapping) else None
    return str(declared or "").strip().lower() == "adjust"


def _scoped_option_runs(
    *,
    plan_id: str,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    session: Any,
) -> list[Any]:
    """This strategy's OWN option runs for the scope, or a fail-closed refusal.

    The read is the platform's scope-derived discovery
    (``OwnedWorkSnapshotService.option_runs_for_scope``), which derives the run
    set from the strategy's own bound attempts - a caller cannot widen it. It is
    the ONE read both the entry gate and the adjust gate ask, so the two can
    never disagree about what this strategy owns.

    The coverage rules are the fail-closed ones the snapshot itself states:
    unknown or truncated discovery, a run whose identity cannot be compared or a
    run whose state row is unreadable all refuse rather than behave as "no runs".
    """
    from backend.strategies.execution_snapshot import OwnedWorkSnapshotService

    try:
        runs, coverage = OwnedWorkSnapshotService(
            session_factory=lambda: session
        ).option_runs_for_scope(
            account_id=str(account_id),
            strategy_id=str(strategy_id),
            environment=str(execution_environment),
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable read is never "no runs"
        raise PlanBindingRefusal(
            "OPTION_STRUCTURE_DISCOVERY_UNKNOWN",
            {
                "plan_id": str(plan_id),
                "strategy_id": str(strategy_id),
                "account_id": str(account_id),
                "execution_environment": str(execution_environment),
                "reason": "option_run_discovery_failed",
                "error": type(exc).__name__,
            },
        ) from exc

    if str(coverage.get("coverage") or "unknown") != "known":
        raise PlanBindingRefusal(
            "OPTION_STRUCTURE_DISCOVERY_UNKNOWN",
            {
                "plan_id": str(plan_id),
                "strategy_id": str(strategy_id),
                "account_id": str(account_id),
                "execution_environment": str(execution_environment),
                "reason": str(coverage.get("reason") or "option_run_discovery_unknown"),
                "truncated": bool(coverage.get("truncated")),
                "count": int(coverage.get("count") or 0),
            },
        )
    return list(runs or [])


def assess_option_entry_admissibility(
    plan: Mapping[str, Any],
    *,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    session: Any,
) -> None:
    """Refuse an option ENTRY this strategy's own durable work already blocks.

    This is the ONE rule, asked from three places with the same answer: before
    the owner is asked to approve (request creation), at admission, and at
    execution under the entry advisory locks. It is side-effect free - it reads
    this strategy's own scope-derived option runs and either returns or raises;
    it never creates a run, a binding or an order.

    A retry of the SAME plan is idempotent through its binding (the execution
    caller checks that first). What this refuses is:

    * an EQUIVALENT structure this strategy already owns, in any status that is
      not provably finished - a restarted strategy, a re-issued signal, or a
      second plan for one structure (``OPTION_STRUCTURE_ALREADY_OPEN``);
    * ANY of this strategy's runs in a status that is not finished, and any run
      whose own records still carry an unresolved protective stage
      (``OPTION_STRUCTURE_UNRESOLVED``). A cleanly ``entered`` DIFFERENT
      structure is the one non-terminal state that stays admissible.

    A run THIS plan is already bound to never blocks this plan: it is the plan's
    own structure, not a second one, so a retry keeps resolving to it exactly as
    the execution path's binding re-read does. (The live lane asks this again
    while its own entry is legitimately in flight, between materialization and
    the release of its withheld steps.)

    Only a structure that is provably finished (``exited`` / ``settled``, with
    no unresolved protective stage) stops blocking. Non-option plans and option
    EXIT plans are not gated at all.

    The discovery is the platform's OWN scope-derived option-run read, and it
    fails closed: unknown or truncated discovery, a run whose identity cannot be
    compared, or a run whose status is outside the durable vocabulary all refuse
    rather than behave as "no runs".
    """
    if not is_option_entry_plan(plan):
        return
    plan_id = str(plan.get("plan_id") or "")
    runs = _scoped_option_runs(
        plan_id=plan_id,
        strategy_id=str(strategy_id),
        account_id=str(account_id),
        execution_environment=str(execution_environment),
        session=session,
    )
    for row in runs:
        run_status = str(row.get("status") or "").strip().lower()
        option_run_id = str(row.get("option_run_id") or "")
        protective_unresolved = bool(row.get("protective_exit_unresolved"))
        if plan_id and plan_id in {str(value) for value in (row.get("plan_ids") or [])}:
            # This plan's OWN run, reached through this plan's own edge.
            continue
        if run_status in _TERMINAL_RUN_STATUSES and not protective_unresolved:
            # Finished structures do not block a new entry.
            continue
        same = _same_structure(plan, row)
        if same is True:
            raise PlanBindingRefusal(
                "OPTION_STRUCTURE_ALREADY_OPEN",
                {
                    "plan_id": str(plan_id),
                    "option_run_id": option_run_id,
                    "option_run_status": run_status or "unknown",
                    "originating_plan_id": row.get("originating_plan_id"),
                    "structure_digest": _frozen_structure_digest(plan) or None,
                    "message": (
                        "this strategy already owns this structure in this account and "
                        "environment; an equivalent structure is never opened twice"
                    ),
                },
            )
        if same is None:
            raise PlanBindingRefusal(
                "OPTION_RUN_IDENTITY_UNKNOWN",
                {
                    "plan_id": str(plan_id),
                    "option_run_id": option_run_id,
                    "option_run_status": run_status or "unknown",
                    "message": (
                        "a held option run of this strategy cannot be compared against "
                        "the frozen structure; refusing to open a second one"
                    ),
                },
            )
        if protective_unresolved or run_status in _UNRESOLVED_RUN_STATUSES:
            raise PlanBindingRefusal(
                "OPTION_STRUCTURE_UNRESOLVED",
                {
                    "plan_id": str(plan_id),
                    "option_run_id": option_run_id,
                    "status": run_status or "unknown",
                    "originating_plan_id": row.get("originating_plan_id"),
                    "message": (
                        "this strategy already owns an option run that is not finished"
                        if not protective_unresolved
                        else "this strategy already owns an option run whose protective "
                        "stage is unresolved; resolve it before opening another structure"
                    ),
                },
            )
        if run_status not in _KNOWN_RUN_STATUSES:
            # A different structure in an unrecognised state is not evidence
            # about THIS plan, but the read is not trustworthy either.
            raise PlanBindingRefusal(
                "OPTION_RUN_STATUS_UNKNOWN",
                {
                    "plan_id": str(plan_id),
                    "option_run_id": option_run_id,
                    "option_run_status": run_status or "unknown",
                    "message": (
                        "an option run of this strategy carries a status outside the "
                        "durable vocabulary; the discovery is not complete"
                    ),
                },
            )


def option_adjust_would_unhedge(plan: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The naked-coverage violation of an adjust's FROZEN target, or ``None``.

    Coverage rule (index options): the frozen structure carries ONE underlying
    and ONE expiry, so the only dimension a short can be covered across is the
    option type. Within each option type, the target's total BUY quantity (the
    protective long) must be at least its total SELL quantity (the short). A type
    whose SELL total exceeds its BUY total leaves that short leg with less
    protection than it covers, so it is a violation - and the legs whose
    magnitude cannot be read are a violation too, because an unreadable size is
    never proof of coverage.

    A frozen ``protection_policy.naked: true`` is the sanctioned declaration of
    an intentional naked structure, and it disables the rule for the whole
    target.

    The rule applies to the TARGET state, never to the intermediate steps: the
    engine's own ordering (reductions first, hedges before shorts) already keeps
    the transient exposure inside one adjust bounded, so only the state the
    structure is left in decides whether it is covered.
    """
    resolved = plan.get("resolved_plan") or {}
    policy = resolved.get("protection_policy")
    if isinstance(policy, Mapping) and bool(policy.get("naked")):
        return None
    bought: dict[str, int] = {}
    sold: dict[str, int] = {}
    unreadable: list[dict[str, Any]] = []
    for leg in _entry_legs(plan):
        option_type = str(leg.get("option_type") or "").strip().upper()
        quantity = _frozen_leg_quantity(leg)
        if quantity is None:
            unreadable.append(
                {
                    "option_type": option_type,
                    "instrument_id": _leg_identity(leg),
                    "reason": "leg_quantity_unreadable",
                }
            )
            continue
        side = str(leg.get("side") or "").strip().upper()
        if side == "BUY":
            bought[option_type] = bought.get(option_type, 0) + quantity
        elif side == "SELL":
            sold[option_type] = sold.get(option_type, 0) + quantity
    violations = [
        {
            "option_type": option_type,
            "buy_quantity": int(bought.get(option_type, 0)),
            "sell_quantity": int(sell_quantity),
        }
        for option_type, sell_quantity in sorted(sold.items())
        if int(sell_quantity) > int(bought.get(option_type, 0))
    ]
    if not violations and not unreadable:
        return None
    return {
        "uncovered": violations,
        "unreadable_legs": unreadable,
        "message": (
            "the desired state leaves a short leg with less protective long coverage "
            "of the same option type; a structure is never left naked unless its "
            "frozen protection policy declares it"
        ),
    }


def _frozen_leg_quantity(leg: Mapping[str, Any]) -> Optional[int]:
    """One frozen leg's unsigned magnitude, or ``None`` when it cannot be read."""
    for key in ("quantity", "signed_quantity"):
        try:
            value = int(leg.get(key))
        except (TypeError, ValueError):
            continue
        return abs(value)
    return None


#: The plan-execution states the adjust takeover rule understands. Only
#: ``finished`` lets anything be re-derived on top of a plan's own work.
PLAN_EXECUTION_FINISHED = "finished"
PLAN_EXECUTION_IN_FLIGHT = "in_flight"
PLAN_EXECUTION_UNKNOWN = "unknown"

#: The trail event that means a step was COMMITTED to the runtime and has no
#: outcome yet. Mirrors ``backend.strategies.execution_snapshot``'s pending
#: vocabulary and the executor's own ``PLAN_ALREADY_EXECUTED`` proof.
_PLAN_TRAIL_SUBMITTED = "submitted"

#: The outcome words that can close a paper order, and the runtime statuses
#: that agree with them. ``failed`` is deliberately absent: it is the executor's
#: word for an UNKNOWN result, not proof that the order stopped.
_PLAN_TRAIL_TERMINAL_OUTCOMES = frozenset({"filled", "rejected", "cancelled", "no_op"})
_PAPER_ORDER_PENDING_STATUSES = frozenset({"pending", "open", "partially_filled"})
_PAPER_ORDER_TERMINAL_STATUSES = frozenset(
    {"filled", "cancelled", "rejected", "expired"}
)


def _paper_order_status(paper_order_id: str, *, session: Any) -> Optional[str]:
    """The current status of one paper order, or ``None`` when unreadable."""
    if not paper_order_id:
        return None
    try:
        row = session.execute(
            text("SELECT status FROM public.paper_orders WHERE order_id = :order_id"),
            {"order_id": str(paper_order_id)},
        ).first()
    except Exception:
        return None
    if row is None:
        return None
    return str(row[0] or "").strip().lower() or None


def _plan_step_is_closed(
    event: str, paper_order_id: str, *, session: Any
) -> tuple[bool, Optional[str]]:
    """Whether one outcome is enough to prove its paper order stopped.

    A no-op never reaches the runtime. Every order-backed outcome is checked
    against the order's CURRENT status: a partial fill is closed only after that
    remainder is terminal, and an unreadable runtime row is never promoted to
    terminal evidence.
    """
    event = str(event or "").strip().lower()
    if event == "no_op":
        return True, None
    if event not in _PLAN_TRAIL_TERMINAL_OUTCOMES and event != "partially_filled":
        return False, None
    if event == "rejected" and not paper_order_id:
        # A gate rejection was never committed to the runtime, so there is no
        # order to reconcile and the step itself is terminal.
        return True, None
    if not paper_order_id:
        if event in ("filled", "cancelled"):
            # The outcome word is terminal evidence itself for an older or
            # orderless trail; only ``failed`` remains an unknown result.
            return True, None
        return False, "paper_order_id_missing"
    status = _paper_order_status(paper_order_id, session=session)
    if status is None:
        return False, "paper_order_status_unreadable"
    if status in _PAPER_ORDER_PENDING_STATUSES:
        return False, "paper_order_still_working"
    if status not in _PAPER_ORDER_TERMINAL_STATUSES:
        return False, "paper_order_status_unknown"
    # A terminal runtime row is the proof that closes even an earlier partial.
    return True, None


def option_run_ledger_consistent(
    run: Any,
    plan_ids: Any,
    *,
    session: Any,
) -> bool:
    """Whether run trades account for every fill in the named plans' trails.

    The comparison is by paper order id, so a repeated reader cannot mistake a
    second copy of a trade for payment of the same fill. A missing, unreadable,
    wrong-symbol or wrong-side trade is inconsistent; this reader never repairs
    the ledger or invents evidence.
    """
    wanted = [str(plan_id) for plan_id in (plan_ids or []) if str(plan_id or "")]
    try:
        raw_trades = (
            getattr(run, "trades", None)
            if hasattr(run, "trades")
            else (run or {}).get("trades")
        )
        if isinstance(raw_trades, str):
            raw_trades = json.loads(raw_trades)
        trades = [
            dict(trade or {})
            for trade in (raw_trades or [])
            if isinstance(trade, Mapping)
        ]
        placeholders = ", ".join(f":plan{index}" for index in range(len(wanted)))
        params = {f"plan{index}": plan_id for index, plan_id in enumerate(wanted)}
        rows = session.execute(
            text(
                "SELECT plan_id, step_no, event, paper_order_id, filled_quantity, detail "
                f"FROM strategy_plan_execution_events WHERE plan_id IN ({placeholders}) "
                "ORDER BY created_at, id"
            ),
            params,
        ).all()
    except Exception:
        return False
    if not wanted:
        return True

    sides: dict[tuple[str, int], str] = {}
    expected: dict[str, dict[str, Any]] = {}
    fill_rows: list[tuple[str, int, str, str, int, Mapping[str, Any]]] = []
    # Scan submissions first: two rows written in one transaction can share
    # created_at, and id is not an execution-order key.
    for row in rows:
        if str(row[2] or "").strip().lower() != "submitted":
            continue
        raw_detail = row[5] or {}
        if isinstance(raw_detail, str):
            raw_detail = json.loads(raw_detail)
        sides[(str(row[0] or ""), int(row[1] or 0))] = str(
            dict(raw_detail).get("side") or ""
        ).strip().upper()
    for row in rows:
        event = str(row[2] or "").strip().lower()
        if event == "submitted":
            continue
        fill_rows.append(
            (str(row[0] or ""), int(row[1] or 0), event, str(row[3] or ""), int(row[4] or 0), row[5] or {})
        )
    for plan_id, step_no, event, order_id, filled, raw_detail in fill_rows:
        try:
            filled = int(filled or 0)
        except (TypeError, ValueError):
            return False
        if isinstance(raw_detail, str):
            raw_detail = json.loads(raw_detail)
        detail = dict(raw_detail)
        if event not in ("filled", "partially_filled") or filled <= 0:
            continue
        if not order_id:
            return False
        symbol = str(detail.get("tradingsymbol") or "").strip().upper()
        if not symbol:
            return False
        evidence = expected.setdefault(
            order_id,
            {"quantity": 0, "tradingsymbol": symbol, "transaction_type": ""},
        )
        if evidence["tradingsymbol"] != symbol:
            return False
        evidence["quantity"] += filled
        side = sides.get((plan_id, step_no), "")
        if side:
            evidence["transaction_type"] = side

    actual: dict[str, dict[str, Any]] = {}
    for trade in trades:
        order_id = str(trade.get("order_id") or "")
        if not order_id or order_id not in expected:
            continue
        symbol = str(trade.get("tradingsymbol") or "").strip().upper()
        side = str(trade.get("transaction_type") or "").strip().upper()
        try:
            quantity = int(trade.get("quantity") or 0)
        except (TypeError, ValueError):
            return False
        row = actual.setdefault(
            order_id,
            {"quantity": 0, "tradingsymbol": symbol, "transaction_type": side},
        )
        row["quantity"] += abs(quantity)
        if row["tradingsymbol"] != symbol or row["transaction_type"] != side:
            return False
    return all(actual.get(order_id) == evidence for order_id, evidence in expected.items())


def option_plan_execution_state(plan_id: str, *, session: Any) -> Dict[str, Any]:
    """Whether ONE plan's own execution has FINISHED, from durable records only.

    The signal is the plan's OWN append-only trail
    (``strategy_plan_execution_events``) - the same trail the executor's
    ``PLAN_ALREADY_EXECUTED`` guard reads and the owned-work snapshot folds. No
    clock, no caller assertion and no new storage: a plan is finished exactly
    when its own committed work has all been answered.

    * ``finished`` - the plan committed at least one submission and every
      order-backed pass is proven terminal in the paper runtime. A
      ``partially_filled`` outcome stays open until that exact paper order is
      cancelled/terminal; an unreadable order is ``unknown``.
    * ``in_flight`` - a committed submission has NO outcome yet: the broker may or
      may not have taken it, so nothing may be re-derived on top of it.
    * ``unknown`` - no committed submission, or the trail cannot be read. The plan
      may still be driven, so its work is never taken over.
    """
    plan = str(plan_id or "")
    if not plan:
        return {"state": PLAN_EXECUTION_UNKNOWN, "evidence": {"reason": "plan_id_missing"}}
    try:
        rows = session.execute(
            text(
                "SELECT step_no, event, paper_order_id FROM strategy_plan_execution_events "
                "WHERE plan_id = :plan ORDER BY created_at, id"
            ),
            {"plan": plan},
        ).all()
    except Exception as exc:  # noqa: BLE001 - an unreadable trail is never "finished"
        return {
            "state": PLAN_EXECUTION_UNKNOWN,
            "evidence": {
                "plan_id": plan,
                "reason": "plan_trail_unreadable",
                "error": type(exc).__name__,
            },
        }
    # A step is closed by a TERMINAL outcome AND the runtime agreeing that its
    # paper order stopped. The two layers make the fold independent of row order
    # and prevent a later fill from landing behind a "partially filled" outcome.
    submitted: set[int] = set()
    closed: set[int] = set()
    unreadable_reasons: list[str] = []
    for row in rows:
        step_no = int(row[0] or 0)
        event = str(row[1] or "")
        if event == _PLAN_TRAIL_SUBMITTED:
            submitted.add(step_no)
            continue
        closed_by_step, reason = _plan_step_is_closed(event, str(row[2] or ""), session=session)
        if closed_by_step:
            closed.add(step_no)
        elif reason == "paper_order_still_working":
            # This is exactly in-flight evidence, not an unreadable read: leave
            # the submitted step unresolved so the fold reports ``in_flight``.
            pass
        else:
            unreadable_reasons.append(reason or f"outcome_not_terminal:{event}")
    evidence = {
        "plan_id": plan,
        "events": len(rows),
        "submitted_events": len(submitted),
        "steps": sorted(submitted | closed),
    }
    if unreadable_reasons:
        return {
            "state": PLAN_EXECUTION_UNKNOWN,
            "evidence": {**evidence, "reasons": sorted(set(unreadable_reasons))},
        }
    if not rows or not submitted:
        return {
            "state": PLAN_EXECUTION_UNKNOWN,
            "evidence": {**evidence, "reason": "no_committed_submission"},
        }
    unresolved = sorted(submitted - closed)
    if unresolved:
        return {
            "state": PLAN_EXECUTION_IN_FLIGHT,
            "evidence": {**evidence, "unresolved_steps": unresolved},
        }
    return {"state": PLAN_EXECUTION_FINISHED, "evidence": evidence}


def option_adjust_owner_plan_ids(option_run_id: str, *, session: Any) -> List[str]:
    """The plans whose binding owns ONE run's adjust phase, oldest first."""
    rows = session.execute(
        text(
            "SELECT plan_id FROM public.strategy_plan_option_runs "
            "WHERE option_run_id = :run AND phase = 'adjust' "
            "ORDER BY created_at, plan_id"
        ),
        {"run": str(option_run_id)},
    ).all()
    return [str(row[0]) for row in rows if str(row[0] or "")]


def option_run_bound_plan_ids(option_run_id: str, *, session: Any) -> List[str]:
    """Every plan edge that owns work in ONE option run, oldest first."""
    rows = session.execute(
        text(
            "SELECT plan_id FROM public.strategy_plan_option_runs "
            "WHERE option_run_id = :run ORDER BY created_at, plan_id"
        ),
        {"run": str(option_run_id)},
    ).all()
    return [str(row[0]) for row in rows if str(row[0] or "")]


def option_adjust_owner_state(
    option_run_id: str, *, session: Any, exclude_plan_ids: Any = ()
) -> Dict[str, Any]:
    """The combined execution state of the plans that own ONE run's adjust phase.

    ``exclude_plan_ids`` names the plan(s) whose OWN attempt is being re-driven -
    a plan never supersedes itself, and an unstarted attempt of the caller's own
    is not a predecessor. ``finished`` then requires EVERY remaining plan bound to
    the run's adjust phase to have finished executing, so an adjust is never taken
    over while a predecessor could still be submitting. A run whose adjust phase
    is reached from NO adjust edge is ``unknown`` (the platform cannot prove who
    moved it), and an unreadable read is ``unknown`` too - never "no owners".
    """
    excluded = {str(value) for value in (exclude_plan_ids or ()) if str(value)}
    option_run = str(option_run_id or "")
    try:
        bound = option_adjust_owner_plan_ids(option_run, session=session)
    except Exception as exc:  # noqa: BLE001 - unreadable ownership is not "none"
        return {
            "state": PLAN_EXECUTION_UNKNOWN,
            "plan_ids": [],
            "plans": {},
            "reason": "adjust_owner_read_failed",
            "error": type(exc).__name__,
        }
    if not bound:
        return {
            "state": PLAN_EXECUTION_UNKNOWN,
            "plan_ids": [],
            "plans": {},
            "reason": "no_adjust_binding",
        }
    owners = [owner for owner in bound if owner not in excluded]
    if not owners:
        # The caller's own edge is the ONLY thing bound to this adjust phase: its
        # own attempt, with nothing to supersede.
        return {"state": PLAN_EXECUTION_FINISHED, "plan_ids": list(bound), "plans": {}}
    plans = {owner: option_plan_execution_state(owner, session=session) for owner in owners}
    states = [str(entry.get("state") or "") for entry in plans.values()]
    if PLAN_EXECUTION_IN_FLIGHT in states:
        state = PLAN_EXECUTION_IN_FLIGHT
    elif PLAN_EXECUTION_UNKNOWN in states:
        state = PLAN_EXECUTION_UNKNOWN
    else:
        state = PLAN_EXECUTION_FINISHED
    return {"state": state, "plan_ids": list(bound), "plans": plans}


def assess_option_adjust_admissibility(
    plan: Mapping[str, Any],
    *,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    session: Any,
) -> None:
    """Refuse an option ADJUST that must not run against the run it names.

    The mirror of :func:`assess_option_entry_admissibility`, asked from the same
    early callers (before the owner is asked to approve, at admission, at
    execution) and answered from the SAME scope-derived discovery. It is
    side-effect free: it reads this strategy's own option runs and either returns
    or raises.

    What it refuses, each BY NAME:

    * the referenced run is not one of THIS strategy's runs in this account and
      environment (``OPTION_ADJUSTMENT_RUN_NOT_OWNED``) - a reference is a
      lookup key, never authority, so a run that the platform cannot reach
      through this strategy's own binding edges is not a run this plan may
      mutate;
    * the run is already being adjusted by ANOTHER plan
      (``OPTION_RUN_ADJUST_IN_FLIGHT``) or moved on entirely
      (``OPTION_RUN_STATE_CHANGED``). Two in-flight states are admissible: the
      run's own ``adjusting`` state reached through THIS plan's edge (a retry of
      the same plan), and an ``adjusting`` state whose owning plan(s) have
      provably FINISHED executing - a withheld or partially filled adjust is
      SUPERSEDED, not repaired by hand, because the successor re-derives every
      delta from the run's own confirmed fills and therefore executes only the
      remainder. A plan still submitting, or an owner set the platform cannot
      read, keeps refusing;
    * the run's held generation is not the one the plan froze
      (``OPTION_ADJUSTMENT_STALE_BASIS``). The approved target is never
      re-derived against a newer structure;
    * the run's own records still own an unresolved protective stage
      (``OPTION_PROTECTIVE_EXIT_UNRESOLVED``);
    * ANY OTHER run of this strategy is unresolved
      (``OPTION_STRUCTURE_UNRESOLVED``);
    * the frozen desired state would leave a short leg short of protective long
      coverage (``OPTION_ADJUSTMENT_WOULD_UNHEDGE``), unless the frozen
      protection policy declares the structure naked.

    Unknown discovery refuses (``OPTION_STRUCTURE_DISCOVERY_UNKNOWN``), exactly
    as the entry gate does. Non-adjust plans are not gated at all.
    """
    if not is_option_adjust_plan(plan):
        return
    plan_id = str(plan.get("plan_id") or "")
    uncovered = option_adjust_would_unhedge(plan)
    if uncovered is not None:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_WOULD_UNHEDGE",
            {
                "plan_id": plan_id,
                "structure_id": str((plan.get("resolved_plan") or {}).get("structure_id") or ""),
                **uncovered,
            },
        )
    runs = _scoped_option_runs(
        plan_id=plan_id,
        strategy_id=str(strategy_id),
        account_id=str(account_id),
        execution_environment=str(execution_environment),
        session=session,
    )
    block = _frozen_option_run_block(plan)
    option_run_id = str(block.get("option_run_id") or "").strip()
    referenced = None
    for row in runs:
        if str(row.get("option_run_id") or "") == option_run_id:
            referenced = row
            break
    if referenced is None:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_RUN_NOT_OWNED",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "strategy_id": str(strategy_id),
                "account_id": str(account_id),
                "execution_environment": str(execution_environment),
                "message": (
                    "an adjust may only mutate a structure this strategy owns in this "
                    "account and environment; this run is reached from no such binding"
                ),
            },
        )
    # The scope projection intentionally reports lifecycle shape, not the full
    # ledger. The one ledger rule needs the run's own trades, so read them from
    # the same durable row before any takeover decision is made.
    try:
        ledger_row = session.execute(
            text(
                "SELECT trades FROM public.option_run_states "
                "WHERE strategy_run_id = :run"
            ),
            {"run": option_run_id},
        ).first()
    except Exception as exc:
        raise PlanBindingRefusal(
            "OPTION_RUN_LEDGER_INCOMPLETE",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "reason": "ledger_read_failed",
                "error": type(exc).__name__,
            },
        ) from exc
    if ledger_row is None:
        raise PlanBindingRefusal(
            "OPTION_RUN_MISSING",
            {"plan_id": plan_id, "option_run_id": option_run_id},
        )
    referenced = {**dict(referenced), "trades": ledger_row[0]}
    run_status = str(referenced.get("status") or "").strip().lower()
    owned_by_this_plan = bool(plan_id) and plan_id in {
        str(value) for value in (referenced.get("plan_ids") or [])
    }
    if run_status == "adjusting":
        if not owned_by_this_plan:
            # A SECOND plan may SUPERSEDE this in-flight adjust only while the
            # plan(s) that own it have provably FINISHED executing: a withheld or
            # partially filled adjust is then the new plan's STARTING POINT, and
            # because every delta is re-derived from the run's own confirmed fills
            # only the remainder executes. Anything else - a plan still submitting,
            # a plan that never committed a submission, or an unreadable owner set -
            # refuses, because the same work must never be submitted twice.
            owner_state = option_adjust_owner_state(
                option_run_id, session=session, exclude_plan_ids=(plan_id,)
            )
            if str(owner_state.get("state") or "") != PLAN_EXECUTION_FINISHED:
                raise PlanBindingRefusal(
                    "OPTION_RUN_ADJUST_IN_FLIGHT",
                    {
                        "plan_id": plan_id,
                        "option_run_id": option_run_id,
                        "option_run_status": run_status,
                        "adjust_owner_state": owner_state,
                        "message": (
                            "this run's in-flight adjust is not provably finished: "
                            "another plan still owns its work, its own trail cannot "
                            "prove the pass closed, or its ownership cannot be read. "
                            "A successor re-derives every delta from the run's own "
                            "confirmed fills, so it is admitted only once nothing of "
                            "the previous adjust can still be submitting."
                        ),
                    },
                )
    elif run_status != "entered":
        raise PlanBindingRefusal(
            "OPTION_RUN_STATE_CHANGED",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "option_run_status": run_status or "unknown",
                "message": "an adjust mutates the run's HELD structure; this run is not entered",
            },
        )
    basis = block.get("based_on_generation")
    try:
        based_on_generation = int(basis)
    except (TypeError, ValueError):
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_BASIS_REQUIRED",
            {"plan_id": plan_id, "option_run_id": option_run_id, "based_on_generation": basis},
        ) from None
    held_generation = int(referenced.get("structure_generation") or 1)
    if based_on_generation != held_generation:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_STALE_BASIS",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "based_on_generation": based_on_generation,
                "structure_generation": held_generation,
                "message": (
                    "the run has moved to a different leg generation than the one this "
                    "adjust was approved against; it is never re-derived against a newer one"
                ),
            },
        )
    try:
        bound_plan_ids = option_run_bound_plan_ids(option_run_id, session=session)
        if not option_run_ledger_consistent(referenced, bound_plan_ids, session=session):
            raise PlanBindingRefusal(
                "OPTION_RUN_LEDGER_INCOMPLETE",
                {
                    "plan_id": plan_id,
                    "option_run_id": option_run_id,
                    "bound_plan_ids": bound_plan_ids,
                    "message": (
                        "the run's confirmed fills do not reconcile with its bound "
                        "plan trails; it is never mutated or silently repaired"
                    ),
                },
            )
    except PlanBindingRefusal:
        raise
    except Exception as exc:
        raise PlanBindingRefusal(
            "OPTION_RUN_LEDGER_INCOMPLETE",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "reason": "ledger_read_failed",
                "error": type(exc).__name__,
            },
        ) from exc
    if bool(referenced.get("protective_exit_unresolved")):
        raise PlanBindingRefusal(
            "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "option_run_status": run_status,
                "message": (
                    "a protective exit stage is unresolved for this run; it is "
                    "reconciled from the platform's own pre-send records before the "
                    "structure is mutated"
                ),
            },
        )
    # The run being adjusted is EXCLUDED here: it is the work this plan owns, and
    # an in-flight adjust on it is the one state a retry resolves through. Every
    # other run of this strategy that is not provably finished blocks the
    # mutation, because the platform cannot know which structure the strategy
    # means to hold while another one is still moving.
    for row in runs:
        if str(row.get("option_run_id") or "") == option_run_id:
            continue
        other_status = str(row.get("status") or "").strip().lower()
        if bool(row.get("protective_exit_unresolved")) or other_status in _UNRESOLVED_RUN_STATUSES:
            raise PlanBindingRefusal(
                "OPTION_STRUCTURE_UNRESOLVED",
                {
                    "plan_id": plan_id,
                    "option_run_id": str(row.get("option_run_id") or ""),
                    "status": other_status or "unknown",
                    "originating_plan_id": row.get("originating_plan_id"),
                    "message": (
                        "this strategy already owns another option run that is not "
                        "finished; an adjust mutates the structure it names only while "
                        "that is the only unresolved structure"
                    ),
                },
            )


def _entry_legs(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    resolved = plan.get("resolved_plan") or {}
    legs = resolved.get("legs")
    return [dict(leg) for leg in legs] if isinstance(legs, list) else []


def _to_execution_leg(leg: Mapping[str, Any], *, plan_id: str, index: int) -> dict[str, Any]:
    quantity = leg.get("quantity")
    if quantity is None:
        quantity = abs(int(leg.get("signed_quantity") or 0))
    # Lots follow the frozen quantity (ratio x structure_units), not the ratio
    # alone, so a multi-unit structure's run legs describe what they hold.
    lot_size = int(leg.get("lot_size") or 0)
    lots = abs(int(quantity or 0)) // lot_size if lot_size > 0 else leg.get("ratio")
    return {
        "leg_id": f"{plan_id}:{index}",
        "tradingsymbol": str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
        "transaction_type": str(leg.get("side") or "BUY").upper(),
        "quantity": abs(int(quantity or 0)),
        "exchange": str(leg.get("broker_exchange") or leg.get("exchange") or "NFO"),
        "product": leg.get("product"),
        "strike": leg.get("strike"),
        "option_type": leg.get("option_type"),
        "expiry_key": leg.get("expiry"),
        "lot_size": leg.get("lot_size"),
        "lots": lots,
        "metadata": {
            "instrument_id": str(leg.get("instrument_id") or ""),
            "role": "entry",
            "ratio": leg.get("ratio"),
        },
    }


def create_run_from_frozen_plan(
    plan: Mapping[str, Any],
    *,
    store: DurableOptionRunStore,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    worker_run_id: Optional[str],
    db: Any = None,
) -> OptionRunState:
    """Create the durable option run from the plan's FROZEN inputs.

    The chain is never re-resolved here: the plan already pinned every leg, so
    the run is created from exactly those identities (this is why the run's legs
    mirror the frozen legs rather than a fresh proposal).
    """
    plan_id = str(plan.get("plan_id") or "")
    resolved = plan.get("resolved_plan") or {}
    legs = _entry_legs(plan)
    if not legs:
        raise PlanBindingRefusal("OPTION_PLAN_LEGS_MISSING", {"plan_id": plan_id})
    request = OptionRunCreateRequest(
        strategy_name=str(plan.get("strategy_id") or ""),
        product=str(resolved.get("product") or "NRML").upper(),
        # ``index + 1`` matches the executor's step numbering (``enumerate(..., start=1)``),
        # so a leg id in the run and a step id in the plan trail name the same leg.
        legs=[
            _to_execution_leg(leg, plan_id=plan_id, index=index + 1)
            for index, leg in enumerate(legs)
        ],
        protection={
            "expiry_policy": resolved.get("expiry_policy"),
            "max_loss": resolved.get("max_loss"),
            "protection_policy": resolved.get("protection_policy"),
            "structure_digest": resolved.get("structure_digest"),
            "structure_units": resolved.get("structure_units"),
            "structure_id": resolved.get("structure_id"),
            "underlying": resolved.get("underlying"),
        },
        metadata={
            "strategy_id": str(strategy_id),
            "account_id": str(account_id),
            "execution_environment": str(execution_environment),
            "worker_run_id": None if worker_run_id is None else str(worker_run_id),
            "plan_id": plan_id,
            "source": "hosted_plan_execution",
        },
    )
    return store.create_run(request, db=db)


def _validate_exit_reference(
    plan: Mapping[str, Any],
    *,
    run: OptionRunState,
    binding: Mapping[str, Any],
    strategy_id: str,
    account_id: str,
    execution_environment: str,
) -> None:
    """An exit plan may only close the structure the platform bound it to."""
    plan_id = str(plan.get("plan_id") or "")
    if (
        str(binding.get("strategy_id")) != str(strategy_id)
        or str(binding.get("account_id")) != str(account_id)
        or str(binding.get("execution_environment")) != str(execution_environment)
    ):
        raise PlanBindingRefusal(
            "OPTION_EXIT_SCOPE_MISMATCH",
            {
                "plan_id": plan_id,
                "option_run_id": run.strategy_run_id,
                "binding_strategy_id": str(binding.get("strategy_id")),
                "binding_account_id": str(binding.get("account_id")),
                "binding_environment": str(binding.get("execution_environment")),
                "plan_strategy_id": str(strategy_id),
                "plan_account_id": str(account_id),
                "plan_environment": str(execution_environment),
            },
        )
    held = {_run_leg_identity(leg): leg for leg in run.legs}
    mismatched: list[dict[str, Any]] = []
    for leg in _entry_legs(plan):
        identity = _leg_identity(leg)
        held_leg = held.get(identity)
        if held_leg is None:
            mismatched.append({"instrument_id": identity, "reason": "not_in_bound_run"})
            continue
        run_side = str(held_leg.get("transaction_type") or "").upper()
        plan_side = str(leg.get("side") or "").upper()
        if run_side and plan_side and run_side == plan_side:
            # Closing a structure moves the OPPOSITE way to the position it holds.
            mismatched.append(
                {
                    "instrument_id": identity,
                    "reason": "same_direction_as_open_leg",
                    "run_side": run_side,
                    "plan_side": plan_side,
                }
            )
    if mismatched:
        raise PlanBindingRefusal(
            "OPTION_EXIT_LEG_MISMATCH",
            {"plan_id": plan_id, "option_run_id": run.strategy_run_id, "mismatched": mismatched},
        )


def _validate_adjust_reference(
    plan: Mapping[str, Any],
    *,
    run: OptionRunState,
    binding: Mapping[str, Any],
    strategy_id: str,
    account_id: str,
    execution_environment: str,
) -> None:
    """An adjust plan may only mutate the structure the platform bound it to.

    The scope checks are the exit edge's own - strategy, account and environment
    of the ENTRY binding, never of the caller's claim. The leg check differs in
    exactly the way the semantics differ: an exit must move OPPOSITE to the
    position it holds, while an adjust may ADD to, reduce, or re-open a leg it
    names. What an adjust may never do is touch a leg under a different product,
    or silently adopt a contract the run does not hold under the same identity.
    """
    plan_id = str(plan.get("plan_id") or "")
    if (
        str(binding.get("strategy_id")) != str(strategy_id)
        or str(binding.get("account_id")) != str(account_id)
        or str(binding.get("execution_environment")) != str(execution_environment)
    ):
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_SCOPE_MISMATCH",
            {
                "plan_id": plan_id,
                "option_run_id": run.strategy_run_id,
                "binding_strategy_id": str(binding.get("strategy_id")),
                "binding_account_id": str(binding.get("account_id")),
                "binding_environment": str(binding.get("execution_environment")),
                "plan_strategy_id": str(strategy_id),
                "plan_account_id": str(account_id),
                "plan_environment": str(execution_environment),
            },
        )
    held = {_run_leg_identity(leg): leg for leg in run.legs}
    mismatched: list[dict[str, Any]] = []
    for leg in _entry_legs(plan):
        held_leg = held.get(_leg_identity(leg))
        if held_leg is None:
            # A leg the run does not hold is a leg this adjust OPENS: the delta is
            # the whole target, and the hedge gate governs it like any increase.
            continue
        run_product = str(held_leg.get("product") or "").upper()
        plan_product = str(leg.get("product") or "").upper()
        if run_product and plan_product and run_product != plan_product:
            mismatched.append(
                {
                    "instrument_id": _leg_identity(leg),
                    "reason": "product_mismatch",
                    "run_product": run_product,
                    "plan_product": plan_product,
                }
            )
    if mismatched:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_LEG_MISMATCH",
            {"plan_id": plan_id, "option_run_id": run.strategy_run_id, "mismatched": mismatched},
        )


def _resolve_adjust_binding(
    plan: Mapping[str, Any],
    *,
    plan_id: str,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    worker_run_id: Optional[str],
    binding_store: PlanOptionRunBindingStore,
    run_store: DurableOptionRunStore,
) -> dict[str, Any]:
    """Bind an ``adjust`` plan to the run it MUTATES (one generation of it).

    Nothing here trusts the caller: the run is read back, its ownership edge is
    an ENTRY binding of this plan's own scope, and the generation basis the plan
    froze is compared against the run's held generation. The mutation rules
    themselves are NOT restated here: they are the shared admissibility rule
    (``assess_option_adjust_admissibility``), asked inside the transaction that
    writes the edge and under the run's own advisory lock, so the execution path
    and the early callers can never disagree about what may be adjusted - and two
    plans racing to take over ONE in-flight adjust serialize instead of both
    sizing the same remainder. A run that moved on (a completed adjust, a close)
    refuses as a stale basis rather than being re-derived against a newer
    structure - the approved artifact stays the approved artifact.
    """
    block = _frozen_option_run_block(plan)
    option_run_id = str(block.get("option_run_id") or "").strip()
    if not option_run_id:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_REFERENCE_REQUIRED",
            {"plan_id": plan_id, "message": "an adjust plan must reference the option run it changes"},
        )
    if worker_run_id and option_run_id == str(worker_run_id):
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_REFERENCE_IS_WORKER_RUN",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "message": "an option run id is not the hosted worker-run id",
            },
        )
    entry_bindings = [
        row for row in binding_store.list_for_run(option_run_id) if row.get("phase") == "entry"
    ]
    if not entry_bindings:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_RUN_NOT_LAUNCHED",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "message": "no entry plan of this platform launched that option run",
            },
        )
    try:
        run = run_store.get_run(option_run_id)
    except KeyError as exc:
        raise PlanBindingRefusal(
            "OPTION_RUN_MISSING",
            {"plan_id": plan_id, "option_run_id": option_run_id},
        ) from exc
    _validate_adjust_reference(
        plan,
        run=run,
        binding=entry_bindings[0],
        strategy_id=strategy_id,
        account_id=account_id,
        execution_environment=execution_environment,
    )
    # The ONE mutation rule is asked INSIDE the transaction that writes the edge,
    # and on PostgreSQL under the run's OWN advisory lock: two plans racing to take
    # over one in-flight adjust serialize there, and the one that goes second
    # reads the FIRST one's edge as an owner that has not finished executing (it
    # has committed no submission yet), so it refuses instead of both sizing the
    # same remainder against the same fills. The key order is fixed - this plan,
    # then this run - so the two locks can never deadlock.
    session = binding_store.session_factory()
    try:
        if _dialect_name(session) == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"option-bind:{plan_id}"},
            )
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"option-run:{option_run_id}"},
            )
        assess_option_adjust_admissibility(
            plan,
            strategy_id=str(strategy_id),
            account_id=str(account_id),
            execution_environment=str(execution_environment),
            session=session,
        )
        binding = binding_store.bind(
            plan_id=plan_id,
            option_run_id=option_run_id,
            strategy_id=strategy_id,
            account_id=account_id,
            execution_environment=execution_environment,
            phase="adjust",
            worker_run_id=worker_run_id,
            db=session,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    try:
        expected_generation = int(
            _frozen_option_run_block(plan).get("based_on_generation")
        )
    except (TypeError, ValueError):
        expected_generation = None
    return {
        "phase": "adjust",
        "option_run_id": run.strategy_run_id,
        "run": run,
        "binding": binding,
        "expected_structure_generation": expected_generation,
    }


def resolve_plan_option_run(
    plan: Mapping[str, Any],
    *,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    worker_run_id: Optional[str],
    binding_store: PlanOptionRunBindingStore,
    run_store: DurableOptionRunStore,
    default_phase: str = "entry",
) -> dict[str, Any]:
    """Resolve one frozen option-structure plan to the run it executes against.

    Entry: create the run from the frozen legs (once) and bind the plan to it.
    Exit: the frozen ``option_run`` reference is a LOOKUP KEY; ownership,
    environment and exact leg identity are validated against the durable run and
    its entry binding before anything is submitted.
    Adjust: the same discipline, applied to a MUTATION. The run reference is
    again only a lookup key; the run must be this strategy's own ``entered``
    structure, its generation must be the one the plan froze as its basis, and
    it must not be holding an unresolved protective stage. The plan binds with
    ``phase="adjust"`` (insert-only, exactly as an exit edge does).
    """
    plan_id = str(plan.get("plan_id") or "")
    phase = _frozen_phase(plan, default=default_phase)
    existing = binding_store.get(plan_id)
    if existing is not None:
        # A retry of an entry/exit plan resolves to the SAME run.
        if (
            str(existing.get("strategy_id")) != str(strategy_id)
            or str(existing.get("account_id")) != str(account_id)
            or str(existing.get("execution_environment")) != str(execution_environment)
        ):
            raise PlanBindingRefusal(
                "OPTION_PLAN_BINDING_SCOPE_MISMATCH",
                {"plan_id": plan_id, "binding": dict(existing)},
            )
        try:
            run = run_store.get_run(str(existing.get("option_run_id")))
        except KeyError as exc:
            raise PlanBindingRefusal(
                "OPTION_RUN_MISSING",
                {"plan_id": plan_id, "option_run_id": str(existing.get("option_run_id"))},
            ) from exc
        expected_generation = None
        if str(existing.get("phase")) == "adjust":
            try:
                expected_generation = int(
                    _frozen_option_run_block(plan).get("based_on_generation")
                )
            except (TypeError, ValueError):
                expected_generation = None
        return {
            "phase": str(existing.get("phase")),
            "option_run_id": run.strategy_run_id,
            "run": run,
            "binding": dict(existing),
            "expected_structure_generation": expected_generation,
        }

    if phase == "entry":
        return _create_entry_run_atomically(
            plan,
            plan_id=plan_id,
            strategy_id=strategy_id,
            account_id=account_id,
            execution_environment=execution_environment,
            worker_run_id=worker_run_id,
            binding_store=binding_store,
            run_store=run_store,
        )

    if phase == "adjust":
        return _resolve_adjust_binding(
            plan,
            plan_id=plan_id,
            strategy_id=strategy_id,
            account_id=account_id,
            execution_environment=execution_environment,
            worker_run_id=worker_run_id,
            binding_store=binding_store,
            run_store=run_store,
        )

    block = _frozen_option_run_block(plan)
    option_run_id = str(block.get("option_run_id") or "").strip()
    if not option_run_id:
        raise PlanBindingRefusal(
            "OPTION_EXIT_REFERENCE_REQUIRED",
            {"plan_id": plan_id, "message": "an exit plan must reference the option run it closes"},
        )
    if option_run_id == str(worker_run_id or "") and worker_run_id:
        # Catch the exact mistake the contract names: the hosted worker-run id is
        # a different identity from the option-run id.
        raise PlanBindingRefusal(
            "OPTION_EXIT_REFERENCE_IS_WORKER_RUN",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "message": "an option run id is not the hosted worker-run id",
            },
        )
    entry_bindings = [
        row for row in binding_store.list_for_run(option_run_id) if row.get("phase") == "entry"
    ]
    if not entry_bindings:
        raise PlanBindingRefusal(
            "OPTION_EXIT_RUN_NOT_LAUNCHED",
            {
                "plan_id": plan_id,
                "option_run_id": option_run_id,
                "message": "no entry plan of this platform launched that option run",
            },
        )
    try:
        run = run_store.get_run(option_run_id)
    except KeyError as exc:
        raise PlanBindingRefusal(
            "OPTION_RUN_MISSING",
            {"plan_id": plan_id, "option_run_id": option_run_id},
        ) from exc
    _validate_exit_reference(
        plan,
        run=run,
        binding=entry_bindings[0],
        strategy_id=strategy_id,
        account_id=account_id,
        execution_environment=execution_environment,
    )
    binding = binding_store.bind(
        plan_id=plan_id,
        option_run_id=option_run_id,
        strategy_id=strategy_id,
        account_id=account_id,
        execution_environment=execution_environment,
        phase="exit",
        worker_run_id=worker_run_id,
    )
    return {"phase": "exit", "option_run_id": run.strategy_run_id, "run": run, "binding": binding}
def _create_entry_run_atomically(
    plan: Mapping[str, Any],
    *,
    plan_id: str,
    strategy_id: str,
    account_id: str,
    execution_environment: str,
    worker_run_id: Optional[str],
    binding_store: PlanOptionRunBindingStore,
    run_store: DurableOptionRunStore,
) -> dict[str, Any]:
    """Create the run AND its binding in ONE transaction (or find the winner's).

    Two instances resolving the same plan must yield exactly ONE run: the run
    row and the binding are written together under a per-plan advisory lock, and
    a losing insert rolls BOTH back before re-reading the winner. The old
    two-commit shape could leave an orphan run that no plan referenced.
    """
    from sqlalchemy.exc import IntegrityError

    def _existing(existing: Mapping[str, Any]) -> dict[str, Any]:
        if (
            str(existing.get("strategy_id")) != str(strategy_id)
            or str(existing.get("account_id")) != str(account_id)
            or str(existing.get("execution_environment")) != str(execution_environment)
        ):
            raise PlanBindingRefusal(
                "OPTION_PLAN_BINDING_SCOPE_MISMATCH",
                {"plan_id": plan_id, "binding": dict(existing)},
            )
        try:
            run = run_store.get_run(str(existing.get("option_run_id")))
        except KeyError as exc:
            raise PlanBindingRefusal(
                "OPTION_RUN_MISSING",
                {"plan_id": plan_id, "option_run_id": str(existing.get("option_run_id"))},
            ) from exc
        return {
            "phase": str(existing.get("phase")),
            "option_run_id": run.strategy_run_id,
            "run": run,
            "binding": dict(existing),
        }

    session = binding_store.session_factory()
    try:
        if _dialect_name(session) == "postgresql":
            # Serialize this STRATEGY's entry admission before this PLAN's: two
            # concurrent plans that would open the same structure must not both
            # read "no equivalent run" and then both create one. The order is
            # fixed (scope, then plan) so the two locks can never deadlock.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {
                    "key": (
                        f"option-scope:{strategy_id}:{account_id}:"
                        f"{execution_environment}"
                    )
                },
            )
            # Serialize the SAME plan's resolution across instances/restarts.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"option-bind:{plan_id}"},
            )
        existing = binding_store.get(plan_id, db=session)
        if existing is not None:
            session.rollback()
            return _existing(existing)

        # Before a NEW run exists: refuse an option entry this strategy's own
        # durable work already blocks (an equivalent structure, or any run that
        # is not finished). Unknown discovery refuses (never "no runs"). This is
        # the SAME rule the early callers ask, re-read here under the entry locks
        # so it stays the race-safe one.
        assess_option_entry_admissibility(
            plan=plan,
            strategy_id=strategy_id,
            account_id=account_id,
            execution_environment=execution_environment,
            session=session,
        )

        run = create_run_from_frozen_plan(
            plan,
            store=run_store,
            strategy_id=strategy_id,
            account_id=account_id,
            execution_environment=execution_environment,
            worker_run_id=worker_run_id,
            db=session,
        )
        try:
            binding = binding_store.bind(
                plan_id=plan_id,
                option_run_id=run.strategy_run_id,
                strategy_id=strategy_id,
                account_id=account_id,
                execution_environment=execution_environment,
                phase="entry",
                worker_run_id=worker_run_id,
                db=session,
            )
        except IntegrityError:
            # Another instance won the plan: roll back THIS run too, then adopt
            # the winner's binding. No orphan run survives.
            session.rollback()
            winner = binding_store.get(plan_id)
            if winner is None:
                raise PlanBindingRefusal(
                    "OPTION_PLAN_BINDING_MISSING", {"plan_id": plan_id}
                )
            return _existing(winner)
        # The owner row is written in the SAME transaction as the run and its
        # entry edge: a run that exists without a protection owner (or the other
        # way round) is not a state this platform can be left in. The owner is
        # the worker run, so a run created without one refuses by name here
        # rather than committing an ownerless "active" row.
        policy_snapshot = option_protection_policy_snapshot(
            plan.get("resolved_plan") or {}
        )
        OptionProtectionOwnerStore(
            session_factory=binding_store.session_factory
        ).claim(
            run,
            worker_run_id,
            policy_snapshot,
            option_protection_policy_version(policy_snapshot),
            db=session,
        )
        session.commit()
        return {
            "phase": "entry",
            "option_run_id": run.strategy_run_id,
            "run": run,
            "binding": binding,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _dialect_name(session: Any) -> str:
    bind = None
    getter = getattr(session, "get_bind", None)
    if callable(getter):
        try:
            bind = getter()
        except Exception:  # noqa: BLE001 - unknown session shape
            bind = None
    return str(getattr(getattr(bind, "dialect", None), "name", None) or "postgresql")
