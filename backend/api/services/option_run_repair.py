"""Owner repair of partial / cleanup option runs (B2.1b): API-layer glue.

The domain rule - what a run holds from its OWN confirmed fills, and which
bounded action may repair it - lives in ``backend.options.execution.repair``.
This module is only the layer a route needs on top of it: owner/account scoping
of the lookup, the originating hosted job that carries the repair's append-only
audit, and the environment's broker boundary for a residual close. Keeping it
here leaves the router with the two handlers and their response mapping.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from fastapi import HTTPException

from backend.api.services.hosted_strategy_authz import authorize_account_scope
from backend.options.execution.repair import OptionRunRepairService

#: The audit outcome one repair writes. Distinct from ``reconciled`` on purpose:
#: this path never clears the job's block, so it must not read as a reconciliation
#: that happened. Widened by migration ``20260925_000044``.
OPTION_RUN_REPAIR_OUTCOME = "option_run_repair"


def _app_state(request: Any) -> Any:
    """The FastAPI app state, or ``None`` for a request that carries no app."""
    return getattr(getattr(request, "app", None), "state", None)


def option_run_repair_scope(
    repo: Any, owner: str, strategy_id: str, option_run_id: str, session_factory: Any
) -> Dict[str, Any]:
    """Owner-scoped resolution of one option run to its account + environment.

    The caller's ``option_run_id`` is a LOOKUP KEY, never an authority: the run is
    only reachable through a binding edge that says this strategy owns it, so
    another strategy's run id (or a run reachable through no edge) is a 404, and a
    run pinned to an account the operator is not authorized for is a 403. The
    owner check mirrors the router's own scoping (foreign and missing are
    indistinguishable) so nothing is looked up before the caller owns the
    strategy.
    """
    from backend.options.execution.plan_binding import PlanOptionRunBindingStore

    if repo.get_strategy(owner, strategy_id) is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    scoped = [
        row
        for row in PlanOptionRunBindingStore(session_factory=session_factory).list_for_run(
            str(option_run_id)
        )
        if str(row.get("strategy_id") or "") == str(strategy_id)
    ]
    if not scoped:
        # Foreign and missing are indistinguishable on purpose.
        raise HTTPException(status_code=404, detail="Option run not found")
    accounts = {str(row.get("account_id") or "") for row in scoped}
    environments = {str(row.get("execution_environment") or "") for row in scoped}
    if len(accounts) != 1 or len(environments) != 1:
        # One run claiming two scopes cannot be repaired under one authority.
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "OPTION_RUN_IDENTITY_UNKNOWN",
                "option_run_id": str(option_run_id),
                "accounts": sorted(accounts),
                "environments": sorted(environments),
            },
        )
    account_id = next(iter(accounts))
    authorize_account_scope(account_id)
    return {
        "option_run_id": str(option_run_id),
        "account_id": account_id,
        "execution_environment": next(iter(environments)),
        "bindings": scoped,
    }


def build_option_run_repair_service(request: Any, session_factory: Any) -> OptionRunRepairService:
    """The repair service over the SAME durable store and staged-exit engine.

    No second engine: the assessment reads the run's own confirmed fills through
    ``StagedStructureExit``, and a residual close is submitted through it too.
    """
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.repair import option_adjust_owner_reader
    from backend.options.execution.repair import (
        option_run_ledger_consistency_reader,
    )
    from backend.options.protection.staged_exit import StagedStructureExit

    run_store = getattr(_app_state(request), "option_run_store", None)
    if run_store is None:
        run_store = DurableOptionRunStore(session_factory=session_factory)
    staged = StagedStructureExit(session_factory=session_factory, run_store=run_store)
    # The owner reader is what lets an ``adjusting`` run be classified: it is the
    # SAME shared rule the adjust gate asks, so the repair path cannot disagree
    # about whether the owning plan may still be submitting.
    return OptionRunRepairService(
        run_store=run_store,
        staged_exit=staged,
        adjust_owner_reader=option_adjust_owner_reader(session_factory),
        ledger_consistency_reader=option_run_ledger_consistency_reader(session_factory),
        unresolved_step_reader=option_run_unresolved_step_reader(session_factory),
    )


def option_run_unresolved_step_reader(
    session_factory: Any,
) -> Callable[[str, int], Dict[str, Any]]:
    """The coordinates of ONE plan step: its own newest trail word and order id.

    The assessment's WHICH-step answer comes from the plan-execution fold; this
    reader only reads the row that fold already read, so an ``adjust_in_flight``
    refusal can name the plan/step an owner has to dispose of (B2.6b §4) without
    a second derivation of ``unresolved``. ``{}`` means the row could not be
    read - never an invented word.
    """
    from sqlalchemy import select

    from backend.strategies.attribution_models import StrategyPlanExecutionEvent

    def _read(plan_id: str, step_no: int) -> Dict[str, Any]:
        with session_factory() as session:
            rows = session.execute(
                select(
                    StrategyPlanExecutionEvent.event,
                    StrategyPlanExecutionEvent.paper_order_id,
                    StrategyPlanExecutionEvent.broker_order_id,
                )
                .where(
                    StrategyPlanExecutionEvent.plan_id == str(plan_id),
                    StrategyPlanExecutionEvent.step_no == int(step_no),
                )
                .order_by(
                    StrategyPlanExecutionEvent.created_at,
                    StrategyPlanExecutionEvent.id,
                )
            ).all()
        if not rows:
            return {}
        event, paper_order_id, broker_order_id = rows[-1]
        return {
            "state": str(event or ""),
            "order_id": str(paper_order_id or broker_order_id or "") or None,
        }

    return _read


def repair_audit_job(repo: Any, run: Any) -> Any:
    """The hosted job that owns this run, or ``None``.

    The repair audit is the job reconciliation's append-only table, so the run must
    resolve to the job that launched it. Resolution happens BEFORE the run moves,
    so an un-auditable run is refused rather than acted on.
    """
    metadata = dict(getattr(run, "metadata", None) or {})
    worker_run_id = str(metadata.get("worker_run_id") or "")
    if not worker_run_id:
        return None
    return repo.get_job_by_run_id(worker_run_id)


def current_protection_owner_run_id(session_factory: Any, run: Any) -> Optional[str]:
    """The worker run CURRENTLY authoritative for this structure, or ``None``.

    The ACTIVE protection owner row is the live relation (B2.4); ``metadata.
    worker_run_id`` is only the creation-time snapshot, which a handover does not
    rewrite. ``None`` means the run has no active owner row (a direct-options-API
    run, or one created before B2.4) and the caller falls back to the snapshot. An
    unreadable row propagates: the caller refuses by name rather than guessing.
    """

    from backend.options.protection.ownership import OptionProtectionOwnerStore

    row = OptionProtectionOwnerStore(session_factory=session_factory).read(
        str(getattr(run, "strategy_run_id", "") or "")
    )
    if not isinstance(row, Mapping) or str(row.get("state") or "") != "active":
        return None
    owner = str(row.get("owner_run_id") or "")
    return owner or None


def record_repair_audit(
    repo: Any,
    *,
    job: Any,
    owner: str,
    strategy_id: str,
    action: str,
    committed: Any,
    assessment: Dict[str, Any],
    submission: Dict[str, Any],
) -> Optional[str]:
    """Append the repair to the same append-only audit the reconciliation uses.

    The row is written AFTER the run actually moved, so it can never claim a repair
    that did not happen; the caller resolved (and required) the job before moving
    the run. Its outcome is ``option_run_repair``, never ``reconciled``: this path
    does not clear the job's block, and the job's reconciliation history must not
    read as if it had.
    """
    row = repo.record_reconciliation(
        job_id=str(job.id),
        strategy_id=str(strategy_id),
        owner_id=str(owner),
        attempt=int(job.attempt or 1),
        run_id=None if not job.run_id else str(job.run_id),
        outcome=OPTION_RUN_REPAIR_OUTCOME,
        reason_code=f"OPTION_RUN_REPAIR_{str(action).upper()}",
        evidence={
            "source": "operator_option_run_repair",
            "option_run_id": str(committed.strategy_run_id),
            "action": str(action),
            "state": str(assessment.get("state") or ""),
            "evidence_digest": str(assessment.get("evidence_digest") or ""),
            "evidence": dict(assessment.get("evidence") or {}),
            "close_plan": list(assessment.get("close_plan") or []),
            "option_run_status": str(committed.status),
            "submission": dict(submission or {}),
        },
        actor_id=str(owner),
    )
    return str(getattr(row, "id", "") or "") or None


def require_residual_close_available(request: Any, run: Any) -> None:
    """Refuse a residual close that could not be submitted, BEFORE the run moves.

    ``live`` is refused by the route (this phase has no live staged-exit
    submission path the operator route may drive); here the paper runtime and the
    run's worker-run identity are checked, so a run is never moved to ``exiting``
    by an action that then has nowhere to go.
    """
    if getattr(_app_state(request), "paper_runtime_service", None) is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    if not str(dict(getattr(run, "metadata", None) or {}).get("worker_run_id") or ""):
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "OPTION_RUN_REPAIR_WORKER_RUN_UNKNOWN",
                "option_run_id": str(run.strategy_run_id),
                "message": "the run carries no worker-run identity to attribute a close to",
            },
        )


def paper_structure_exit_boundary(paper_service: Any):
    """The paper runtime as the staged structure exit's broker boundary.

    Same shape as the live boundary in ``protection_runtime``: one basket leg per
    claim leg, a SERVER-SIDE attribution (never the evaluator's order list), and
    only the closes the exit builder permitted. It cannot increase exposure.
    """
    from backend.algo_runtime.execution_attribution import build_execution_attribution

    async def place_orders(
        *,
        account_id: str,
        worker_run_id: str,
        option_run_id: str,
        structure_digest: str,
        legs: List[Dict[str, Any]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        outcomes: List[Dict[str, Any]] = []
        for index, leg in enumerate(legs):
            leg = dict(leg or {})
            stage = idempotency_key.rsplit(":", 1)[-1][:8].upper()
            attribution = build_execution_attribution(
                execution_mode="paper",
                strategy_run_id=str(worker_run_id),
                strategy_family="options_strategy",
                strategy_name="option_structure_repair",
                account_ref=str(account_id),
                entry_surface="hosted_option_repair",
                source="operator_option_run_repair",
                idempotency_key=str(idempotency_key),
                metadata={
                    "option_run_id": str(option_run_id),
                    "structure_digest": str(structure_digest),
                    "stage_digest": idempotency_key.rsplit(":", 1)[-1],
                    "stage_leg_index": index,
                },
            )
            attribution["client_order_ref"] = str(
                leg.get("client_order_ref") or f"KA{stage}{index + 1:02d}"
            )
            try:
                result = await paper_service.place_order(
                    account_scope=str(account_id),
                    order_payload={
                        "exchange": str(leg.get("exchange") or "NFO"),
                        "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                        "transaction_type": str(leg.get("transaction_type") or ""),
                        "quantity": abs(int(leg.get("quantity") or 0)),
                        "variety": str(leg.get("variety") or "regular"),
                        "product": str(leg.get("product") or "NRML"),
                        "order_type": str(leg.get("order_type") or "MARKET"),
                    },
                    attribution=attribution,
                )
            except Exception as exc:  # noqa: BLE001 - an unplaced leg is not a placed one
                outcomes.append({"index": index, "order_id": None, "error": str(exc)})
                continue
            payload = dict(result or {})
            order = dict(payload.get("order") or {})
            order_id = str(order.get("order_id") or "") or None
            outcomes.append(
                {
                    "index": index,
                    "order_id": order_id,
                    "error": (
                        None
                        if order_id
                        else str(
                            payload.get("reason") or "no order reference returned for this leg"
                        )
                    ),
                }
            )
        return {"legs": outcomes}

    return place_orders


async def submit_residual_close(
    request: Any,
    session_factory: Any,
    *,
    run: Any,
    scope: Dict[str, Any],
) -> Dict[str, Any]:
    """Submit one residual close through the SAME staged-exit engine, on paper.

    ``live`` never reaches here: this phase has no live staged-exit submission path
    that the operator route may drive (the protection runtime's is bound to a live
    protection trigger on a worker run), so the route refuses by name rather than
    inventing one.
    """
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.protection.staged_exit import (
        OWNER_UNREADABLE,
        StagedStructureExit,
    )

    require_residual_close_available(request, run)
    paper_service = getattr(_app_state(request), "paper_runtime_service", None)
    metadata = dict(getattr(run, "metadata", None) or {})
    # The close is ATTRIBUTED to the structure's CURRENT protection owner, not to
    # the creation-time snapshot: after a handover the owner row names the
    # successor while ``metadata.worker_run_id`` still names the predecessor, and
    # the resolver refuses a superseded binding rather than acting for it (B2.4).
    # An unreadable owner row is a named refusal here too - never a silent
    # fallback to the stale binding.
    try:
        attribution_run_id = current_protection_owner_run_id(session_factory, run)
    except Exception as exc:  # noqa: BLE001 - unreadable ownership refuses by name
        return {
            "submitted": False,
            "complete": False,
            "reason": OWNER_UNREADABLE,
            "option_run_id": str(run.strategy_run_id),
            "error": f"{type(exc).__name__}: {exc}",
            "orders": [],
        }
    run_store = getattr(_app_state(request), "option_run_store", None)
    if run_store is None:
        run_store = DurableOptionRunStore(session_factory=session_factory)
    staged = StagedStructureExit(
        session_factory=session_factory,
        run_store=run_store,
        place_orders=paper_structure_exit_boundary(paper_service),
    )
    protection = dict(getattr(run, "protection", None) or {})
    return await staged.submit(
        worker_run={
            "strategy_run_id": str(
                attribution_run_id or metadata.get("worker_run_id") or ""
            ),
            "account_scope": str(scope.get("account_id") or ""),
            "metadata": metadata,
            "runtime_state": {
                "backend_protection": {
                    "structure": {"structure_digest": str(protection.get("structure_digest") or "")}
                }
            },
        },
        trigger={"status": "triggered"},
    )
