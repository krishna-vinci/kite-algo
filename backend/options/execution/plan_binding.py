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
        if phase not in ("entry", "exit"):
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
    if phase in ("entry", "exit"):
        return phase
    if phase:
        raise PlanBindingRefusal(
            "OPTION_PLAN_PHASE_INVALID",
            {"plan_id": str(plan.get("plan_id") or ""), "phase": phase},
        )
    return default


def _entry_legs(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    resolved = plan.get("resolved_plan") or {}
    legs = resolved.get("legs")
    return [dict(leg) for leg in legs] if isinstance(legs, list) else []


def _to_execution_leg(leg: Mapping[str, Any], *, plan_id: str, index: int) -> dict[str, Any]:
    quantity = leg.get("quantity")
    if quantity is None:
        quantity = abs(int(leg.get("signed_quantity") or 0))
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
        "lots": leg.get("ratio"),
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
            # Serialize the SAME plan's resolution across instances/restarts.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"option-bind:{plan_id}"},
            )
        existing = binding_store.get(plan_id, db=session)
        if existing is not None:
            session.rollback()
            return _existing(existing)

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
