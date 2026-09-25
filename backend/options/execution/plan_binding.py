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

from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from backend.app.database import SessionLocal

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

    for row in list(runs or []):
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
            "structure_digest": resolved.get("structure_digest"),
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


def _structure_generation(run: OptionRunState) -> int:
    """The run's held leg generation. Absent means the first one (D-9).

    The counter lives in ``option_run_states.metadata`` JSONB, so a run created
    before adjustments existed (or by a path that never adjusted) reads as
    generation 1 rather than as "unknown": the only thing an adjust needs to
    know is whether the basis it froze is still the generation the run holds.
    """
    metadata = getattr(run, "metadata", None) or {}
    try:
        generation = int(metadata.get("structure_generation") or 1)
    except (TypeError, ValueError):
        return 1
    return generation if generation >= 1 else 1


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
    froze is compared against the run's held generation. A run that moved on (a
    completed adjust, a close) refuses as a stale basis rather than being
    re-derived against a newer structure - the approved artifact stays the
    approved artifact.
    """
    from backend.options.protection.staged_exit import unresolved_stage_claim

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
    status = str(getattr(run, "status", "") or "").strip().lower()
    if status == "adjusting":
        # This plan has no binding yet (an existing one returned earlier), so the
        # in-flight adjust belongs to ANOTHER plan: one transition, one owner.
        raise PlanBindingRefusal(
            "OPTION_RUN_ADJUST_IN_FLIGHT",
            {
                "plan_id": plan_id,
                "option_run_id": run.strategy_run_id,
                "option_run_status": status,
                "message": (
                    "another plan owns this run's in-flight adjust; its delta is "
                    "re-derived from the run's own fills, never from a second plan"
                ),
            },
        )
    if status != "entered":
        raise PlanBindingRefusal(
            "OPTION_RUN_STATE_CHANGED",
            {
                "plan_id": plan_id,
                "option_run_id": run.strategy_run_id,
                "option_run_status": status or "unknown",
                "message": "an adjust mutates the run's HELD structure; this run is not entered",
            },
        )
    basis = block.get("based_on_generation")
    try:
        based_on_generation = int(basis)
    except (TypeError, ValueError):
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_BASIS_REQUIRED",
            {"plan_id": plan_id, "based_on_generation": basis},
        ) from None
    held_generation = _structure_generation(run)
    if based_on_generation != held_generation:
        raise PlanBindingRefusal(
            "OPTION_ADJUSTMENT_STALE_BASIS",
            {
                "plan_id": plan_id,
                "option_run_id": run.strategy_run_id,
                "based_on_generation": based_on_generation,
                "structure_generation": held_generation,
                "message": (
                    "the run has moved to a different leg generation than the one this "
                    "adjust was approved against; it is never re-derived against a newer one"
                ),
            },
        )
    unresolved = unresolved_stage_claim(getattr(run, "orders", None) or [])
    if unresolved is not None:
        raise PlanBindingRefusal(
            "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
            {
                "plan_id": plan_id,
                "option_run_id": run.strategy_run_id,
                "option_run_status": status,
                "stage_digest": str(unresolved.get("stage_digest") or ""),
                "stage_state": str(unresolved.get("state") or ""),
                "stage_attempt": int(unresolved.get("attempt") or 1),
                "message": (
                    "a protective exit stage is unresolved for this run; it is "
                    "reconciled from the platform's own pre-send records before the "
                    "structure is mutated"
                ),
            },
        )
    binding = binding_store.bind(
        plan_id=plan_id,
        option_run_id=option_run_id,
        strategy_id=strategy_id,
        account_id=account_id,
        execution_environment=execution_environment,
        phase="adjust",
        worker_run_id=worker_run_id,
    )
    return {"phase": "adjust", "option_run_id": run.strategy_run_id, "run": run, "binding": binding}


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
        return {"phase": str(existing.get("phase")), "option_run_id": run.strategy_run_id, "run": run, "binding": dict(existing)}

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
