"""Owner repair of partial / cleanup option runs (B2.1b): API-layer glue.

The domain rule - what a run holds from its OWN confirmed fills, and which
bounded action may repair it - lives in ``backend.options.execution.repair``.
This module is only the layer a route needs on top of it: owner/account scoping
of the lookup, the originating hosted job that carries the repair's append-only
audit, and the environment's broker boundary for a residual close. Keeping it
here leaves the router with the two handlers and their response mapping.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from fastapi import HTTPException

from backend.api.services.hosted_strategy_authz import authorize_account_scope
from backend.broker_api.orders.autoslice import should_autoslice
from backend.options.execution.repair import (
    ACTION_OWNER_EXIT,
    STATE_FLAT,
    STATE_RESIDUAL,
    TERMINAL_RUN_STATUSES,
    OptionRunRepairRefusal,
    OptionRunRepairService,
)

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


def paper_structure_exit_boundary(
    paper_service: Any,
    *,
    entry_surface: str = "hosted_option_repair",
    source: str = "operator_option_run_repair",
    strategy_name: str = "option_structure_repair",
):
    """The paper runtime as the staged structure exit's broker boundary.

    Same shape as the live boundary in ``protection_runtime``: one basket leg per
    claim leg, a SERVER-SIDE attribution (never the evaluator's order list), and
    only the closes the exit builder permitted. It cannot increase exposure.

    The attribution stamps are parameters because the SAME paper staged
    submitter serves two owner actions (B2.6b): the governed repair close
    (defaults) and the owner-authorized discretionary exit
    (``hosted_option_owner_exit`` / ``owner_discretionary_exit``). One boundary,
    two truthful attributions.
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
                strategy_name=str(strategy_name),
                account_ref=str(account_id),
                entry_surface=str(entry_surface),
                source=str(source),
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


# ---------------------------------------------------------------------------
# Owner-authorized discretionary exit of ONE option run (B2.6b S2)
# ---------------------------------------------------------------------------
#
# The action's own vocabulary. Everything else is the repair machinery above:
# the SAME assessment (in its owner-exit view), the SAME staged structure exit,
# the SAME run CAS. What differs is only what the §5 contract names, which
# boundary the stage is submitted through, and which refusal a stage failure is
# reported as.

#: The live boundary could not be proven available, so no stage was claimed and
#: nothing was sent. Fail closed BEFORE the run moves.
REASON_EXIT_LIVE_UNAVAILABLE = "OPTION_OWNER_EXIT_LIVE_UNAVAILABLE"

#: The attribution the owner exit stamps: the platform's own broker boundary
#: carries the action, and the ledger must say which action that was.
OWNER_EXIT_ENTRY_SURFACE = "hosted_option_owner_exit"
OWNER_EXIT_SOURCE = "owner_discretionary_exit"
OWNER_EXIT_STRATEGY_NAME = "option_structure_owner_exit"


def owner_exit_attribution_run_id(session_factory: Any, run: Any) -> Optional[str]:
    """The worker run the owner exit is ATTRIBUTED to, or ``None``.

    The structure's CURRENT protection owner row wins when it is readable (B2.4:
    a handover moves the row while the creation-time snapshot keeps naming the
    predecessor), and the run's own creation snapshot is the fallback. Both the
    owner row being ABSENT and it being UNREADABLE fall back, because this action
    only reduces risk: an unknown owner must not block an exit
    (``require_option_protection_owner``'s "reduce-only work and exits stay
    admissible"). No ``caller_worker_run_id`` is ever passed, because the owner
    acts on the STRUCTURE - the superseded-caller rule is about a caller acting
    for a run the row has moved past, not about the platform's own exit.
    """
    try:
        return current_protection_owner_run_id(session_factory, run)
    except Exception:  # noqa: BLE001 - an unreadable owner row is not a blocker
        return None


def live_owner_exit_basket_boundary(request: Any, *, kite: Any):
    """The live basket boundary for the owner exit, stamped as the owner's action.

    The mechanism is the one backend protection already uses
    (``protection_runtime.submit_worker_protection_structure_exit``'s boundary:
    the platform's own broker session, ``OrdersService.place_basket`` and a
    SERVER-SIDE attribution), reproduced here only because the attribution must
    name THIS action (``hosted_option_owner_exit`` / ``owner_discretionary_exit``)
    and that function's stamps are hard-coded to protection. Nothing here reads
    the child's token, and every leg is a close of a leg the run's own evidence
    says it holds, so the boundary cannot increase exposure.
    """
    from uuid import uuid4

    from fastapi import Response

    async def place_orders(
        *,
        account_id: str,
        worker_run_id: str,
        option_run_id: str,
        structure_digest: str,
        legs: List[Dict[str, Any]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        from backend.algo_runtime.execution_attribution import build_execution_attribution
        from backend.broker_api.orders import BasketOrderRequest, OrdersService

        stage = idempotency_key.rsplit(":", 1)[-1][:8].upper()
        payload_orders: List[Dict[str, Any]] = []
        for index, leg in enumerate(legs):
            leg = dict(leg or {})
            client_order_ref = str(
                leg.get("client_order_ref") or f"KA{stage}{index + 1:02d}"
            )
            attribution = build_execution_attribution(
                execution_mode="live",
                strategy_run_id=str(worker_run_id),
                strategy_family="options_strategy",
                strategy_name=OWNER_EXIT_STRATEGY_NAME,
                account_ref=str(account_id),
                entry_surface=OWNER_EXIT_ENTRY_SURFACE,
                source=OWNER_EXIT_SOURCE,
                idempotency_key=str(idempotency_key),
                metadata={
                    "option_run_id": str(option_run_id),
                    "structure_digest": str(structure_digest),
                    "stage_digest": idempotency_key.rsplit(":", 1)[-1],
                    "stage_leg_index": index,
                },
            )
            attribution["client_order_ref"] = client_order_ref
            payload_orders.append(
                {
                    "exchange": str(leg.get("exchange") or "NFO"),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "transaction_type": str(leg.get("transaction_type") or ""),
                    "quantity": abs(int(leg.get("quantity") or 0)),
                    "variety": str(leg.get("variety") or "regular"),
                    "product": str(leg.get("product") or "NRML"),
                    "order_type": str(leg.get("order_type") or "MARKET"),
                    "autoslice": should_autoslice(str(leg.get("exchange") or "")),
                    "attribution": attribution,
                }
            )
        basket = BasketOrderRequest.model_validate(
            {"orders": payload_orders, "all_or_none": False, "dry_run": False}
        )
        service = getattr(
            _app_state(request), "algo_worker_orders_service", None
        ) or OrdersService()
        try:
            result = await service.place_basket(
                kite,
                basket,
                f"option-owner-exit-{uuid4()}",
                session_id=f"backend:option-owner-exit:{option_run_id}",
                idempotency_key=idempotency_key,
                response=Response(),
            )
        except Exception as exc:  # noqa: BLE001 - the stage stays unresolved, never re-sent
            return {
                "legs": [
                    {"index": index, "order_id": None, "error": str(exc)}
                    for index in range(len(payload_orders))
                ]
            }
        payload = result.model_dump(mode="json")
        answers: Dict[int, Dict[str, Any]] = {}
        for position, row in enumerate(list(payload.get("results") or [])):
            if not isinstance(row, Mapping):
                continue
            answers[int(row.get("index", position))] = dict(row)
        return {
            "legs": [
                {
                    "index": index,
                    "order_id": (
                        str(answers[index].get("order_id"))
                        if answers.get(index, {}).get("order_id")
                        else None
                    ),
                    "error": (
                        None
                        if answers.get(index, {}).get("order_id")
                        else "no order reference returned for this leg"
                    ),
                }
                for index in range(len(payload_orders))
            ]
        }

    return place_orders


async def require_owner_exit_boundary(
    request: Any, *, scope: Mapping[str, Any], run: Any
) -> Any:
    """The broker boundary one owner-exit stage will be sent through.

    Fail closed BEFORE the run moves: a stage is only ever submitted through a
    boundary the platform can actually reach. Paper (and ``dry_run``) needs the
    configured paper runtime; ``live`` needs the platform's own broker session
    for the run's account (the same session loader backend protection uses), or
    the injected owner-exit boundary in tests. Missing either is the named
    refusal ``OPTION_OWNER_EXIT_LIVE_UNAVAILABLE`` rather than a claimed stage
    that could never be sent.
    """
    import asyncio

    environment = str(scope.get("execution_environment") or "")
    state = _app_state(request)
    if environment != "live":
        paper_service = getattr(state, "paper_runtime_service", None)
        if paper_service is None:
            raise HTTPException(status_code=503, detail="Paper runtime is not available")
        return paper_structure_exit_boundary(
            paper_service,
            entry_surface=OWNER_EXIT_ENTRY_SURFACE,
            source=OWNER_EXIT_SOURCE,
            strategy_name=OWNER_EXIT_STRATEGY_NAME,
        )
    injected = getattr(state, "option_owner_exit_live_boundary", None)
    if injected is not None:
        return injected
    from backend.api.routers.worker_shared import _load_live_kite_for_account

    try:
        kite = await asyncio.to_thread(
            _load_live_kite_for_account, str(scope.get("account_id") or "")
        )
    except Exception as exc:  # noqa: BLE001 - no session, no send
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": REASON_EXIT_LIVE_UNAVAILABLE,
                "option_run_id": str(getattr(run, "strategy_run_id", "") or ""),
                "execution_environment": "live",
                "reason": type(exc).__name__,
                "message": (
                    "the platform's live broker boundary is not available for this "
                    "account, so no exit stage may be claimed or sent"
                ),
            },
        ) from exc
    return live_owner_exit_basket_boundary(request, kite=kite)


async def submit_owner_exit_stage(
    request: Any,
    session_factory: Any,
    *,
    run: Any,
    scope: Mapping[str, Any],
    boundary: Any,
) -> Dict[str, Any]:
    """Submit ONE owner-exit stage through the staged structure exit engine.

    The engine re-derives the bounded actions from the run's OWN confirmed fills
    (shorts first, a hedge only once its short is proven closed), claims the stage
    durably before the send, and never re-sends an unresolved one. The action
    contributes only the boundary and the attribution the stage carries.
    """
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.protection.staged_exit import StagedStructureExit

    metadata = dict(getattr(run, "metadata", None) or {})
    attribution_run_id = owner_exit_attribution_run_id(session_factory, run)
    run_store = getattr(_app_state(request), "option_run_store", None)
    if run_store is None:
        run_store = DurableOptionRunStore(session_factory=session_factory)
    staged = StagedStructureExit(
        session_factory=session_factory,
        run_store=run_store,
        place_orders=boundary,
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
                    "structure": {
                        "structure_digest": str(protection.get("structure_digest") or "")
                    }
                }
            },
        },
        trigger={"status": "triggered"},
    )


async def run_owner_exit(
    request: Any,
    session_factory: Any,
    repo: Any,
    *,
    strategy_id: str,
    option_run_id: str,
    reason: str,
    actor: str,
    evidence_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """One owner-authorized discretionary exit of ONE run: gate, take, submit, audit.

    The route and the flatten orchestration (B2.6b S3) both call THIS function, so
    there is exactly one implementation of the S2 exit: the same gates, the same
    run CAS, the same staged submitter and the same audit. A route supplies the
    digest the owner read; flatten omits it, and the digest is then taken from the
    assessment read here rather than invented.

    The run's own transition is the ownership token: exactly one caller takes
    ``entered`` / ``exiting`` and submits ONE stage of the derived close plan.
    Nothing is submitted when a gate refuses, and the run is never marked
    ``exited`` merely because a broker accepted a stage - completion is the run's
    own fills proving it flat.

    Refusals are raised as ``OptionRunRepairRefusal`` (the caller maps them to the
    §5 names) or ``HTTPException`` (a boundary the platform cannot reach, so no
    stage may be claimed).
    """
    scope = option_run_repair_scope(
        repo, str(actor), strategy_id, option_run_id, session_factory
    )
    service = build_option_run_repair_service(request, session_factory)
    # The specific gates are asked on a fresh read BEFORE the transition: an
    # unresolved stage or an unfinished adjust outranks "your evidence is stale"
    # as the explanation, and nothing has moved yet either way.
    observed = service.assessment(option_run_id, owner_exit=True)
    owner_exit_gates(observed)
    pinned_digest = str(evidence_digest or observed.get("evidence_digest") or "")
    next_run, assessment = service.plan(
        option_run_id=option_run_id,
        action=ACTION_OWNER_EXIT,
        evidence_digest=pinned_digest,
        owner_exit=True,
    )
    state = str(assessment.get("state") or "")
    observed_status = str(assessment.get("status") or "")
    # Everything that can refuse happens BEFORE the run moves: an unavailable
    # live boundary must never leave a claimed stage that nothing can send.
    boundary = None
    if state == STATE_RESIDUAL:
        boundary = await require_owner_exit_boundary(request, scope=scope, run=next_run)
    if state == STATE_FLAT and observed_status in TERMINAL_RUN_STATUSES:
        # Already past the exit: report it complete, do not write the same
        # terminal status again.
        committed = next_run
    else:
        committed = service.commit(next_run, allowed_from=observed_status)
    action_id = str(uuid4())
    submission: Any = {}
    if boundary is not None:
        submission = await submit_owner_exit_stage(
            request, session_factory, run=committed, scope=scope, boundary=boundary
        )
    refusal = None if state == STATE_FLAT else owner_exit_submission_refusal(submission)
    if state == STATE_FLAT:
        status = "complete"
    elif refusal is None and submission.get("submitted"):
        status = "accepted"
    else:
        status = "blocked"
    audit_id = record_owner_exit_audit(
        session_factory,
        repo,
        strategy_id=str(strategy_id),
        run=committed,
        action_id=action_id,
        assessment=assessment,
        submission=submission,
        reason=str(reason or ""),
        actor=str(actor),
    )
    evidence = dict(assessment.get("evidence") or {})
    return {
        "status": status,
        "action_id": action_id,
        "option_run_id": str(committed.strategy_run_id),
        "run_status": str(committed.status),
        "state": state,
        "evidence_digest": str(assessment.get("evidence_digest") or ""),
        "items": owner_exit_stage_items(submission),
        "refusal": refusal,
        "audit_id": audit_id,
        "submission": dict(submission or {}),
        # Flatten reads these two directly: they are what makes "the hedge is
        # withheld until its short is PROVEN closed" observable in the operation's
        # own manifest instead of only inside the run.
        "shorts_proven_closed": bool(evidence.get("shorts_proven_closed")),
        "withheld_hedges": [
            dict(row or {}) for row in (assessment.get("withheld_hedges") or [])
        ],
    }


def owner_exit_view(service: Any, option_run_id: str) -> Dict[str, Any]:
    """The §5 ``GET .../exit`` body: the evidence one POST will be pinned to.

    ``state`` is DERIVED from the run's own confirmed fills, never asserted by a
    caller. ``adjust_owner_state`` is the shared takeover rule's own word for a
    run whose status is ``adjusting`` - the only status an adjust can own - and
    ``finished`` otherwise, because no adjust is in flight for it (the same
    condition the adjust gate asks). ``protective_stage_state`` is the run's own
    stage record: ``resolved`` when nothing is unresolved, else the claim's state.
    """
    assessment = service.assessment(option_run_id, owner_exit=True)
    return {
        "option_run_id": str(assessment.get("option_run_id") or ""),
        "status": str(assessment.get("status") or ""),
        "state": str(assessment.get("state") or ""),
        "reason_code": assessment.get("reason_code"),
        "reasons": [str(value) for value in (assessment.get("reasons") or [])],
        **_owner_exit_evidence_view(assessment),
        "evidence_digest": str(assessment.get("evidence_digest") or ""),
    }


def _owner_exit_evidence_view(assessment: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = dict(assessment.get("evidence") or {})
    detail = dict(assessment.get("detail") or {})
    status = str(assessment.get("status") or "")
    owner_state = dict(evidence.get("adjust_owner") or {})
    unresolved = evidence.get("unresolved_stage") or None
    close_plan = [
        {
            "tradingsymbol": str(order.get("tradingsymbol") or ""),
            "transaction_type": str(order.get("transaction_type") or ""),
            "quantity": int(order.get("quantity") or 0),
            "exchange": None
            if order.get("exchange") is None
            else str(order.get("exchange")),
            "product": None
            if order.get("product") is None
            else str(order.get("product")),
            "order_type": None
            if order.get("order_type") is None
            else str(order.get("order_type")),
        }
        for order in (assessment.get("close_plan") or [])
    ]
    if status == "adjusting":
        # The ONLY status an adjust phase can own: the shared takeover rule's own
        # word, and its own named reason when the rule could not be asked.
        adjust_state = str(owner_state.get("state") or "unknown")
        adjust_reason = str(owner_state.get("reason") or "") or None
    else:
        # No adjust owns this run, so none can be in flight - the same condition
        # the adjust gate itself asks.
        adjust_state = "finished"
        adjust_reason = "no_adjust_owner"
    return {
        "adjust_owner_state": adjust_state,
        "adjust_owner_reason": adjust_reason,
        "protective_stage_state": (
            "resolved"
            if unresolved is None
            else str(unresolved.get("state") or "unknown")
        ),
        "close_plan": close_plan,
        "shorts_proven_closed": bool(evidence.get("shorts_proven_closed")),
        "naked_short_quantity": int(detail.get("naked_short_quantity") or 0),
        "withheld_hedges": [
            dict(row or {})
            for row in (
                assessment.get("withheld_hedges")
                or detail.get("withheld_hedges")
                or []
            )
        ],
        "waiting_reason": _owner_exit_waiting_reason(assessment, evidence, close_plan),
    }


def _owner_exit_waiting_reason(
    assessment: Mapping[str, Any], evidence: Mapping[str, Any], close_plan: List[Any]
) -> Optional[str]:
    """Why a residual run has no stage to submit right now, or ``None``.

    Proof-based waiting is expected, not an error (§7): a clear read with an
    empty ``close_plan`` is the platform saying "this run still holds something,
    and nothing is releasable yet" - most often a submitted stage whose fills
    have not landed, or a hedge withheld until its short is proven closed.
    """
    if str(assessment.get("state") or "") != "residual" or close_plan:
        return None
    if evidence.get("outstanding_buy") or evidence.get("outstanding_sell"):
        return "orders_outstanding"
    if not evidence.get("shorts_proven_closed"):
        return "shorts_not_proven_closed"
    return "no_permitted_action"


def _owner_exit_reason_for(
    *, reason_code: str, status: str, reasons: Sequence[str]
) -> Optional[str]:
    """The §5 owner-exit name for one assessment verdict, or ``None``.

    ONE mapping, so the GET-side gates and the POST's plan/commit refusals can
    never disagree about what a verdict is called.
    """
    from backend.options.execution import repair as repair_module

    code = str(reason_code or "")
    if code == repair_module.REASON_EVIDENCE_CHANGED:
        return repair_module.REASON_EXIT_EVIDENCE_CHANGED
    if code == repair_module.REASON_STATE_CHANGED:
        return repair_module.REASON_EXIT_STATE_CHANGED
    if code == repair_module.REASON_NOT_REPAIRABLE:
        return (
            repair_module.REASON_EXIT_BEFORE_ENTRY
            if str(status or "") in repair_module.BEFORE_ENTRY_RUN_STATUSES
            else repair_module.REASON_EXIT_NOT_APPLICABLE
        )
    if code == repair_module.REASON_AMBIGUOUS:
        if repair_module.REASON_ADJUST_IN_FLIGHT in set(reasons or ()):
            return repair_module.REASON_EXIT_ADJUST_IN_FLIGHT
        if repair_module.REASON_PROTECTIVE_STAGE_UNRESOLVED in set(reasons or ()):
            return repair_module.REASON_EXIT_PROTECTIVE_UNRESOLVED
        return repair_module.REASON_EXIT_EVIDENCE_AMBIGUOUS
    return None


def _owner_exit_detail(
    *,
    option_run_id: str,
    status: str,
    reasons: Sequence[str],
    message: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "option_run_id": str(option_run_id or ""),
        "observed_status": str(status or ""),
        "reasons": [str(value) for value in (reasons or [])],
        "message": str(message or ""),
    }
    payload.update(dict(extra or {}))
    return payload


def owner_exit_gates(assessment: Mapping[str, Any]) -> None:
    """Refuse an assessment the owner exit may not act on, by its §5 name.

    Asked BEFORE the transition, so the most specific refusal wins: an adjust
    that is not provably finished, a stage the run's own records still own, or
    fills the platform cannot explain. A verdict the exit MAY act on passes.
    """
    from backend.options.execution import repair as repair_module

    state = str(assessment.get("state") or "")
    if state not in (repair_module.STATE_AMBIGUOUS, repair_module.STATE_NOT_REPAIRABLE):
        return
    status = str(assessment.get("status") or "")
    reasons = [str(value) for value in (assessment.get("reasons") or [])]
    reason_code = str(assessment.get("reason_code") or "")
    mapped = _owner_exit_reason_for(
        reason_code=reason_code, status=status, reasons=reasons
    )
    if mapped is None:
        return
    raise OptionRunRepairRefusal(
        mapped,
        _owner_exit_detail(
            option_run_id=str(assessment.get("option_run_id") or ""),
            status=status,
            reasons=reasons,
            message={
                repair_module.REASON_EXIT_ADJUST_IN_FLIGHT: (
                    "another plan's adjust is not provably finished; nothing may be "
                    "submitted on top of it"
                ),
                repair_module.REASON_EXIT_PROTECTIVE_UNRESOLVED: (
                    "this run's own records still own an unresolved exit stage; it is "
                    "reconciled before anything new is sent"
                ),
            }.get(mapped, "this run cannot be explained from its own confirmed fills"),
            extra={
                "unattributable_trades": assessment.get("unattributable_trades"),
                "unreadable_fills": assessment.get("unreadable_fills"),
            },
        ),
    )


def owner_exit_refusal(exc: Any, *, status: str = "") -> Any:
    """The §5 owner-exit name for one repair plan/commit refusal.

    A refusal that is already an owner-exit refusal (the live boundary, say)
    passes through untouched.
    """
    reason_code = str(getattr(exc, "reason_code", "") or "")
    detail = dict(getattr(exc, "detail", None) or {})
    mapped = _owner_exit_reason_for(
        reason_code=reason_code,
        status=str(detail.get("status") or status or ""),
        reasons=[str(value) for value in (detail.get("reasons") or [])],
    )
    if mapped is None:
        return exc
    return OptionRunRepairRefusal(
        mapped,
        _owner_exit_detail(
            option_run_id=str(detail.get("option_run_id") or ""),
            status=str(detail.get("status") or status or ""),
            reasons=[str(value) for value in (detail.get("reasons") or [])],
            message=str(detail.get("message") or ""),
            extra={
                key: detail[key]
                for key in ("unattributable_trades", "unreadable_fills")
                if key in detail
            },
        ),
        status_code=int(getattr(exc, "status_code", 409) or 409),
    )


def owner_exit_submission_refusal(submission: Mapping[str, Any]) -> Optional[str]:
    """The §5 name for a stage the engine could not submit, or ``None``.

    A stage that lost the run's claim to another sender is a LOST CAS
    (``OPTION_RUN_STATE_CHANGED``); a claim whose send outcome is unknown leaves
    the run's own records owning the stage (``OPTION_PROTECTIVE_EXIT_UNRESOLVED``);
    an attribution the platform cannot read is ambiguous evidence.
    """
    from backend.options.execution import repair as repair_module

    reason = str(submission.get("reason") or "")
    if submission.get("submitted") or reason in ("submitted", "already_submitted"):
        # The stage is this run's own, durably recorded submission (or the same
        # stage answered again): there is nothing to refuse.
        return None
    if not reason:
        return None
    if reason == "stage_claimed_by_other":
        return repair_module.REASON_EXIT_STATE_CHANGED
    if reason in (
        "stage_send_unknown",
        "stage_claim_failed",
        "stage_record_failed",
        "no_order_boundary",
    ):
        return repair_module.REASON_EXIT_PROTECTIVE_UNRESOLVED
    return repair_module.REASON_EXIT_EVIDENCE_AMBIGUOUS


def record_owner_exit_audit(
    session_factory: Any,
    repo: Any,
    *,
    strategy_id: str,
    run: Any,
    action_id: str,
    assessment: Mapping[str, Any],
    submission: Mapping[str, Any],
    reason: str,
    actor: str,
) -> Optional[str]:
    """Append the exit to the owner-action audit (journal + hosted job record).

    The SAME audit the S1 owner actions write: a strategy-scoped append-only
    journal row, plus the hosted job's reconciliation row when this run resolves
    to one. A job that cannot be resolved yields the journal id rather than an
    invented audit id.
    """
    from backend.api.services.owner_actions import OwnerActionsService

    run_id = str(dict(getattr(run, "metadata", None) or {}).get("worker_run_id") or "")
    service = OwnerActionsService(session_factory=session_factory, repository=repo)
    return service.record_audit(
        {"strategy_id": str(strategy_id)},
        action="owner_exit",
        evidence={
            "action_id": str(action_id),
            "option_run_id": str(getattr(run, "strategy_run_id", "") or ""),
            "run_status": str(getattr(run, "status", "") or ""),
            "state": str(assessment.get("state") or ""),
            "evidence_digest": str(assessment.get("evidence_digest") or ""),
            "evidence": dict(assessment.get("evidence") or {}),
            "close_plan": list(assessment.get("close_plan") or []),
            "submission": dict(submission or {}),
            "reason": str(reason or ""),
            "entry_surface": OWNER_EXIT_ENTRY_SURFACE,
            "source": OWNER_EXIT_SOURCE,
        },
        run_id=run_id or None,
        option_run_id=str(getattr(run, "strategy_run_id", "") or ""),
        actor=str(actor),
    )


def owner_exit_stage_items(submission: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One item per leg of the stage the engine answered for.

    A leg with no broker reference is reported with its named blocker instead of
    being dropped: an exit that quietly loses a leg is a structure left
    half-hedged.
    """
    outcomes = list(submission.get("leg_outcomes") or [])
    stage_digest = str(submission.get("stage_digest") or "") or None
    if not outcomes:
        orders = list(submission.get("orders") or [])
        order_ids = [str(value) for value in (submission.get("order_ids") or [])]
        outcomes = [
            {
                "index": index,
                "tradingsymbol": str(dict(order or {}).get("tradingsymbol") or ""),
                "transaction_type": str(
                    dict(order or {}).get("transaction_type") or ""
                ),
                "quantity": abs(int(dict(order or {}).get("quantity") or 0)),
                "client_order_ref": dict(order or {}).get("client_order_ref"),
                "order_id": order_ids[index] if index < len(order_ids) else None,
                "error": None,
            }
            for index, order in enumerate(orders)
        ]
    items: List[Dict[str, Any]] = []
    for row in outcomes:
        row = dict(row or {})
        order_id = row.get("order_id")
        items.append(
            {
                "tradingsymbol": str(row.get("tradingsymbol") or ""),
                "transaction_type": str(row.get("transaction_type") or ""),
                "quantity": abs(int(row.get("quantity") or 0)),
                "order_id": None if order_id in (None, "") else str(order_id),
                "client_order_ref": row.get("client_order_ref"),
                "stage_digest": stage_digest,
                "state": "submitted" if order_id else "unknown",
                "reason_code": None
                if order_id
                else str(row.get("error") or "no_order_reference_returned"),
            }
        )
    return items
