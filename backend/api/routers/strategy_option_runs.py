"""Read-only owner API for a strategy's option runs (B2.6a).

Two GET routes and no mutation:

* ``GET /api/strategies/{strategy_id}/option-runs`` - the strategy's own option
  runs for its derived ``(account, environment)`` scope, with an explicit
  coverage verdict so an incomplete read is never rendered as "no structures".
* ``GET /api/strategies/{strategy_id}/option-runs/{option_run_id}`` - the same
  run object plus its binding edges, frozen policies, named refusals, and the
  greeks / P&L this phase can honestly answer.

What this router deliberately does NOT do:

* It never takes the owning account or environment from the caller. The account
  is the canonical strategy's own ``account_scope`` and the environment is the
  strategy's own default execution mode (an explicit ``environment`` query is
  validated and can only narrow, never widen, the read).
* It does not re-derive run state. The run set comes from the platform's own
  scope-derived discovery (``OwnedWorkSnapshotService.option_runs_for_scope``),
  a leg's open quantity from the durable run's own confirmed fills
  (``StagedStructureExit.own_open_by_leg``), and refusals from the durable
  execution-request rows. Nothing here places an order or moves a run.
* It reports a value it cannot prove as ``null`` plus a named reason - never as
  a zero, an empty list, or an invented aggregate.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from backend.api.routers.strategies import require_strategy_owner
from backend.api.schemas.strategy_option_runs import (
    OptionRunDetailResponse,
    OptionRunEdgeResponse,
    OptionRunFrozenResponse,
    OptionRunGreeksResponse,
    OptionRunLegResponse,
    OptionRunListResponse,
    OptionRunPnlResponse,
    OptionRunRefusalResponse,
    OptionRunResponse,
)
from backend.api.services.hosted_strategy_authz import authorize_account_scope
from backend.options.execution.plan_binding import _leg_identity, _run_leg_identity
from backend.options.execution.repair import REPAIRABLE_RUN_STATUSES
from backend.strategies.attribution import EXECUTION_ENVIRONMENTS
from backend.strategies.attribution_models import StrategyPlan, StrategyPlanOptionRun
from backend.strategies.repository import SqlAlchemyStrategyRepository

router = APIRouter(prefix="/strategies", tags=["Hosted strategies (operator)"])

logger = logging.getLogger(__name__)

__all__ = ["router"]

#: Coverage vocabulary. ``unknown`` means the list is NOT complete.
COVERAGE_KNOWN = "known"
COVERAGE_UNKNOWN = "unknown"

#: Named reasons a derived value is unavailable. "no_reusable_read" is the
#: honest answer when the platform has no read that already computes the value
#: for an option run; the route refuses to invent one.
NO_REUSABLE_READ = "no_reusable_read"
OPTION_RUN_UNREADABLE = "option_run_unreadable"

#: A leg's own state, derived from the run's own confirmed fills first.
LEG_OPEN = "open"
LEG_PENDING = "pending"
LEG_FAILED = "failed"
LEG_FLAT = "flat"

#: The statuses the governed repair path can act on. Imported, not restated, so
#: this read can never disagree with the repair route about what "repairable"
#: means.
REPAIRABLE_STATUSES = frozenset(str(value) for value in REPAIRABLE_RUN_STATUSES)

#: The refusal window: the detail response carries at most this many, newest
#: first, scanned from a bounded slice of the strategy's recent requests.
REFUSAL_LIMIT = 20
REFUSAL_SCAN_LIMIT = 200


def _option_runs_db(request: Request):
    """Sessionmaker for the hosted-strategy tables (injectable for tests)."""
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _repository(
    request: Request, session_factory: Any = Depends(_option_runs_db)
) -> SqlAlchemyStrategyRepository:
    return SqlAlchemyStrategyRepository(session_factory)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _owned_strategy(repo: SqlAlchemyStrategyRepository, owner: str, strategy_id: str):
    row = repo.get_strategy(owner, strategy_id)
    if row is None:
        # Foreign and missing are indistinguishable on purpose.
        raise HTTPException(status_code=404, detail="Strategy not found")
    return row


def _environment_for(hosted: Any, requested: Optional[str]) -> str:
    """The environment for this read, derived from the strategy itself.

    An explicit ``environment`` is validated and honoured; omitted, the
    strategy's own default execution mode decides. Both use the same vocabulary
    (``live`` / ``paper`` / ``dry_run``), so the read can never be widened to a
    scope the strategy does not act in.
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


def _option_run_scope(
    repo: SqlAlchemyStrategyRepository,
    owner: str,
    strategy_id: str,
    requested_environment: Optional[str],
) -> tuple:
    """The strategy's own ``(account_id, environment)``, never a caller's claim."""
    hosted = _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id) or hosted
    account_id = str(
        getattr(canonical, "account_scope", "")
        or getattr(hosted, "default_account_scope", "")
    ).strip()
    if not account_id:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(account_id)
    return account_id, _environment_for(hosted, requested_environment)


def _snapshot_service(request: Request, session_factory: Any):
    injected = getattr(request.app.state, "owned_work_snapshot_service", None)
    if injected is not None:
        return injected
    from backend.strategies.execution_snapshot import OwnedWorkSnapshotService

    return OwnedWorkSnapshotService(session_factory=session_factory)


def _run_store(request: Request, session_factory: Any):
    """The durable option-run store the own-fill evidence is read through."""
    injected = getattr(request.app.state, "option_run_store", None)
    if injected is not None:
        return injected
    from backend.options.execution.durable_store import DurableOptionRunStore

    return DurableOptionRunStore(session_factory=session_factory)


def _plan_order(plan: Any) -> tuple:
    """A stable "newest first" key for a plan row.

    Compared as text so a naive/aware timestamp mix (SQLite vs PostgreSQL) can
    never raise here: ISO-8601 strings sort chronologically.
    """
    return (
        str(getattr(plan, "created_at", None) or ""),
        str(getattr(plan, "plan_id", "")),
    )


class _RunReads:
    """Lazy, cached reads of the evidence the run list needs per run."""

    def __init__(self, session_factory: Any, run_store: Any) -> None:
        self._session_factory = session_factory
        self._run_store = run_store
        self._plans_by_run: Dict[str, List[Any]] = {}
        self._runs: Dict[str, Any] = {}
        self._own_open: Dict[str, Optional[Dict[str, int]]] = {}

    def plans(self, row: Mapping[str, Any]) -> List[Any]:
        run_id = str(row.get("option_run_id") or "")
        if run_id in self._plans_by_run:
            return self._plans_by_run[run_id]
        plan_ids = [
            str(value) for value in (row.get("plan_ids") or []) if str(value or "")
        ]
        plans: List[Any] = []
        if plan_ids:
            try:
                with self._session_factory() as session:
                    plans = list(
                        session.execute(
                            select(StrategyPlan).where(
                                StrategyPlan.plan_id.in_(plan_ids)
                            )
                        )
                        .scalars()
                        .all()
                    )
            except Exception:  # noqa: BLE001 - an unreadable plan block is not fatal here
                logger.exception(
                    "option_run_plans_read_failed", extra={"option_run_id": run_id}
                )
                plans = []
        self._plans_by_run[run_id] = plans
        return plans

    def _run(self, option_run_id: str) -> Any:
        """The durable run, or ``None`` when it cannot be read.

        ``None`` is "unreadable", never "empty": every caller turns it into a
        named unavailability rather than a zero.
        """
        if option_run_id in self._runs:
            return self._runs[option_run_id]
        try:
            run = self._run_store.get_run(str(option_run_id))
        except Exception:  # noqa: BLE001 - missing and unreadable are both "unknown"
            run = None
        self._runs[option_run_id] = run
        return run

    def own_open(self, option_run_id: str) -> Optional[Dict[str, int]]:
        """Signed open quantity per leg from the run's OWN confirmed fills."""
        if option_run_id in self._own_open:
            return self._own_open[option_run_id]
        from backend.options.protection.staged_exit import StagedStructureExit

        run = self._run(option_run_id)
        value: Optional[Dict[str, int]] = None
        if run is not None:
            try:
                value = dict(StagedStructureExit.own_open_by_leg(run))
            except Exception:  # noqa: BLE001 - unreadable fills are not "flat"
                logger.exception(
                    "option_run_own_fills_read_failed",
                    extra={"option_run_id": option_run_id},
                )
                value = None
        self._own_open[option_run_id] = value
        return value

    def durable_run(self, option_run_id: str) -> Any:
        return self._run(option_run_id)


def _source_plan(row: Mapping[str, Any], plans: List[Any]) -> Any:
    """The plan whose frozen legs describe the structure the run HOLDS now.

    The run's held ``structure_digest`` decides it when a plan froze the same
    shape (an adjust rewrites the run's legs and freezes the new digest); the
    originating entry plan is the fallback, and the newest plan the last.
    """
    if not plans:
        return None
    held = str(row.get("structure_digest") or "")
    if held:
        matching = [
            plan
            for plan in plans
            if str(
                (getattr(plan, "resolved_plan", None) or {}).get("structure_digest")
                or ""
            )
            == held
        ]
        if matching:
            return max(matching, key=_plan_order)
    by_id = {str(getattr(plan, "plan_id", "")): plan for plan in plans}
    origin = by_id.get(str(row.get("originating_plan_id") or ""))
    if origin is not None:
        return origin
    return max(plans, key=_plan_order)


def _frozen(row: Mapping[str, Any], plans: List[Any]) -> OptionRunFrozenResponse:
    """The frozen policies/limits, originating plan first and latest as fallback."""
    by_id = {str(getattr(plan, "plan_id", "")): plan for plan in plans}
    ordered = [by_id.get(str(row.get("originating_plan_id") or ""))]
    if plans:
        ordered.append(max(plans, key=_plan_order))

    def pick(field: str) -> Any:
        for plan in ordered:
            if plan is None:
                continue
            resolved = getattr(plan, "resolved_plan", None) or {}
            value = resolved.get(field)
            if value:
                return value
        return None

    policy = pick("protection_policy")
    max_loss = pick("max_loss")
    expiry_policy = pick("expiry_policy")
    return OptionRunFrozenResponse(
        protection_policy=dict(policy) if isinstance(policy, Mapping) else None,
        max_loss=dict(max_loss) if isinstance(max_loss, Mapping) else None,
        expiry_policy=None if expiry_policy in (None, "") else str(expiry_policy),
    )


def _plan_legs(plan: Any) -> List[Dict[str, Any]]:
    if plan is None:
        return []
    resolved = getattr(plan, "resolved_plan", None) or {}
    legs = resolved.get("legs")
    return [dict(leg) for leg in legs] if isinstance(legs, list) else []


def _leg_state(
    leg_id: str,
    own_open: Optional[int],
    *,
    completed: set,
    failed: set,
    pending: set,
) -> str:
    """One leg's state from the run's own evidence, never a guess at "flat"."""
    if own_open is not None and int(own_open) != 0:
        return LEG_OPEN
    if leg_id and leg_id in failed:
        return LEG_FAILED
    if leg_id and leg_id in pending:
        return LEG_PENDING
    if own_open is not None:
        return LEG_FLAT
    # The run's own fills were unreadable: a completed leg reads open and
    # anything else reads pending - "flat" would be a claim without evidence.
    if leg_id and leg_id in completed:
        return LEG_OPEN
    return LEG_PENDING


def _leg_out(
    *,
    leg_id: str,
    run_leg: Mapping[str, Any],
    frozen: Mapping[str, Any],
    own_open: Optional[int],
    completed: set,
    failed: set,
    pending: set,
) -> OptionRunLegResponse:
    role_raw = frozen.get("role")
    role = str(role_raw).strip().lower() if role_raw not in (None, "") else None
    ratio = _as_int(frozen.get("ratio")) or 1
    if ratio <= 0:
        ratio = 1
    quantity = _as_int(run_leg.get("quantity"))
    if quantity is None:
        quantity = _as_int(frozen.get("quantity")) or 0
    return OptionRunLegResponse(
        leg_id=leg_id,
        tradingsymbol=str(
            run_leg.get("tradingsymbol")
            or frozen.get("broker_symbol")
            or frozen.get("tradingsymbol")
            or ""
        ),
        side=str(run_leg.get("transaction_type") or frozen.get("side") or "").upper(),
        role=role,
        ratio=ratio,
        quantity=quantity,
        own_open_quantity=own_open,
        state=_leg_state(
            leg_id, own_open, completed=completed, failed=failed, pending=pending
        ),
    )


def _legs_out(
    row: Mapping[str, Any],
    *,
    plan_legs: List[Dict[str, Any]],
    plan_id: str,
    own_open_by_leg: Optional[Mapping[str, int]],
) -> List[OptionRunLegResponse]:
    """The run's legs: what it holds now, with the frozen leg as coverage.

    The durable run is the authority for which legs exist and how much they
    target; the run's plan supplies ``role`` / ``ratio`` (the durable leg model
    does not carry the coverage role). When the run's own state could not be
    read the frozen legs describe the intended structure and every own quantity
    stays ``None``.
    """
    run_legs = [
        dict(leg) for leg in (row.get("legs") or []) if isinstance(leg, Mapping)
    ]
    completed = {str(value) for value in (row.get("completed_legs") or [])}
    failed = {str(value) for value in (row.get("failed_legs") or [])}
    pending = {str(value) for value in (row.get("pending_legs") or [])}

    frozen_by_identity: Dict[str, Mapping[str, Any]] = {}
    for leg in plan_legs:
        identity = _leg_identity(leg)
        if identity and identity not in frozen_by_identity:
            frozen_by_identity[identity] = leg

    readable_fills = own_open_by_leg is not None
    out: List[OptionRunLegResponse] = []
    if run_legs:
        for index, leg in enumerate(run_legs):
            leg_id = str(
                leg.get("leg_id") or (f"{plan_id}:{index + 1}" if plan_id else "")
            )
            identity = _run_leg_identity(leg)
            out.append(
                _leg_out(
                    leg_id=leg_id,
                    run_leg=leg,
                    frozen=frozen_by_identity.get(identity) or {},
                    own_open=own_open_by_leg.get(leg_id) if readable_fills else None,
                    completed=completed,
                    failed=failed,
                    pending=pending,
                )
            )
        return out
    for index, leg in enumerate(plan_legs):
        leg_id = f"{plan_id}:{index + 1}" if plan_id else f"leg_{index + 1}"
        out.append(
            _leg_out(
                leg_id=leg_id,
                run_leg={},
                frozen=leg,
                own_open=own_open_by_leg.get(leg_id) if readable_fills else None,
                completed=completed,
                failed=failed,
                pending=pending,
            )
        )
    return out


def _option_run_out(row: Mapping[str, Any], reads: _RunReads) -> OptionRunResponse:
    run_id = str(row.get("option_run_id") or "")
    plans = reads.plans(row)
    source = _source_plan(row, plans)
    plan_legs = _plan_legs(source)
    plan_id = str(
        getattr(source, "plan_id", "") or row.get("originating_plan_id") or ""
    )
    status = str(row.get("status") or "unknown")
    return OptionRunResponse(
        option_run_id=run_id,
        status=status,
        structure_generation=_as_int(row.get("structure_generation")) or 1,
        structure_digest=str(row.get("structure_digest") or ""),
        underlying=str(row.get("underlying") or ""),
        expiry=str(row.get("expiry") or ""),
        product=str(row.get("product") or ""),
        protective_exit_unresolved=bool(row.get("protective_exit_unresolved")),
        coverage=str(row.get("coverage") or COVERAGE_UNKNOWN),
        legs=_legs_out(
            row,
            plan_legs=plan_legs,
            plan_id=plan_id,
            own_open_by_leg=reads.own_open(run_id),
        ),
        repairable=status in REPAIRABLE_STATUSES,
        protection_owner=None,
    )


def _owned_run_binding(
    session_factory: Any,
    *,
    strategy_id: str,
    account_id: str,
    environment: str,
    option_run_id: str,
) -> List[Any]:
    """This strategy's binding edges to one run, oldest first (possibly empty)."""
    with session_factory() as session:
        return list(
            session.execute(
                select(StrategyPlanOptionRun)
                .where(
                    StrategyPlanOptionRun.option_run_id == str(option_run_id),
                    StrategyPlanOptionRun.strategy_id == str(strategy_id),
                    StrategyPlanOptionRun.account_id == str(account_id),
                    StrategyPlanOptionRun.execution_environment == str(environment),
                )
                .order_by(
                    StrategyPlanOptionRun.created_at,
                    StrategyPlanOptionRun.plan_id,
                )
            )
            .scalars()
            .all()
        )


def _degraded_row(option_run_id: str, edges: List[Any]) -> Dict[str, Any]:
    """A run this strategy owns whose own state could not be read.

    The binding edge proves ownership; the run's state does not exist as far as
    this read can tell, so the object is reported with ``unknown`` status and
    ``unknown`` coverage rather than a 404 the UI would misread as "not yours".
    """
    plan_ids = [str(edge.plan_id) for edge in edges]
    entry = next(
        (edge for edge in edges if str(edge.phase) == "entry"),
        edges[0] if edges else None,
    )
    return {
        "option_run_id": str(option_run_id),
        "plan_ids": plan_ids,
        "originating_plan_id": None if entry is None else str(entry.plan_id),
        "originating_phase": None if entry is None else str(entry.phase),
        "phase": None if entry is None else str(entry.phase),
        "worker_run_id": None,
        "underlying": "",
        "expiry": "",
        "structure_id": "",
        "structure_digest": "",
        "structure_generation": 1,
        "expiry_policy": "",
        "product": "",
        "status": "unknown",
        "legs": [],
        "completed_legs": [],
        "pending_legs": [],
        "failed_legs": [],
        "protective_exit_unresolved": False,
        "coverage": COVERAGE_UNKNOWN,
    }


def _option_structure_plan_ids(session_factory: Any, plan_ids: List[str]) -> set:
    """Which of ``plan_ids`` are frozen ``option_structure`` plans."""
    wanted = [str(value) for value in plan_ids if str(value or "")]
    if not wanted:
        return set()
    with session_factory() as session:
        rows = session.execute(
            select(
                StrategyPlan.plan_id,
                StrategyPlan.plan_kind,
                StrategyPlan.resolved_plan,
            ).where(StrategyPlan.plan_id.in_(wanted))
        ).all()
    found = set()
    for plan_id, plan_kind, resolved_plan in rows:
        target_kind = str((resolved_plan or {}).get("target_kind") or "")
        if (
            target_kind == "option_structure"
            or str(plan_kind or "") == "option_structure"
        ):
            found.add(str(plan_id))
    return found


def _refusal_stage(row: Mapping[str, Any]) -> Optional[str]:
    for key in ("refusal_detail", "execution_detail"):
        detail = row.get(key)
        if isinstance(detail, Mapping) and detail.get("stage") not in (None, ""):
            return str(detail.get("stage"))
    return None


def _refusals(
    session_factory: Any, *, strategy_id: str
) -> List[OptionRunRefusalResponse]:
    """Named refusals of this strategy's option-structure plans, newest first.

    Scope is the STRATEGY (as the API contract states), filtered to plans that
    are frozen ``option_structure`` plans and to requests that actually carry a
    refusal code, so an approved or executed request is never shown as refused.
    """
    from backend.strategies.execution_requests import ExecutionRequestService

    try:
        rows = ExecutionRequestService(session_factory).list_for_strategy(
            str(strategy_id), limit=REFUSAL_SCAN_LIMIT
        )
    except Exception:  # noqa: BLE001 - a refusal read is evidence, not the run itself
        logger.exception(
            "option_run_refusals_read_failed", extra={"strategy_id": str(strategy_id)}
        )
        return []
    option_plans = _option_structure_plan_ids(
        session_factory, [str(row.get("plan_id") or "") for row in rows]
    )
    out: List[OptionRunRefusalResponse] = []
    for row in rows:  # already newest first
        plan_id = str(row.get("plan_id") or "")
        refusal_code = str(row.get("refusal_code") or "")
        if not refusal_code or plan_id not in option_plans:
            continue
        out.append(
            OptionRunRefusalResponse(
                request_id=str(row.get("request_id") or ""),
                plan_id=plan_id,
                refusal_code=refusal_code,
                stage=_refusal_stage(row),
                detail=dict(row.get("refusal_detail") or {}),
                at=_iso(row.get("created_at")),
            )
        )
        if len(out) >= REFUSAL_LIMIT:
            break
    return out


def _greeks() -> OptionRunGreeksResponse:
    """Run-level greeks are not derivable from any existing run read.

    ``OptionsMarketService.get_greeks`` derives them per CONTRACT from a live
    session's chain snapshot - it is not a run-level aggregate and needs an
    active session, so this reports the named absence instead of summing a chain
    the owner did not ask for.
    """
    return OptionRunGreeksResponse(available=False, reason=NO_REUSABLE_READ)


def _pnl(run: Any) -> OptionRunPnlResponse:
    """Premium / MTM from the run's own protection metrics, or a named absence."""
    if run is None:
        return OptionRunPnlResponse(available=False, reason=OPTION_RUN_UNREADABLE)
    from backend.options.protection.metrics import derive_protection_metrics

    try:
        metrics = derive_protection_metrics(run)
    except Exception:  # noqa: BLE001 - an unreadable metric snapshot is not a value
        logger.exception(
            "option_run_metrics_read_failed",
            extra={"option_run_id": str(getattr(run, "strategy_run_id", "") or "")},
        )
        return OptionRunPnlResponse(available=False, reason=OPTION_RUN_UNREADABLE)
    premium = _as_float(metrics.get("combined_premium"))
    mtm = _as_float(metrics.get("strategy_mtm"))
    if premium is None or mtm is None:
        # The run carries no recorded premium/MTM snapshot: report the absence by
        # name, keeping whichever single side WAS recorded rather than rounding
        # it to zero.
        return OptionRunPnlResponse(
            available=False, reason=NO_REUSABLE_READ, premium=premium, mtm=mtm
        )
    return OptionRunPnlResponse(available=True, reason="", premium=premium, mtm=mtm)


@router.get("/{strategy_id}/option-runs", response_model=OptionRunListResponse)
async def list_option_runs(
    strategy_id: str,
    request: Request,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_option_runs_db),
):
    """The strategy's own option runs, plus an explicit coverage verdict.

    An incomplete read reports ``coverage: "unknown"`` with a named reason. It
    never reports an empty list as if it were the complete set of structures the
    strategy owns.
    """
    account_id, env = _option_run_scope(repo, owner, strategy_id, environment)
    service = _snapshot_service(request, session_factory)
    rows, coverage = service.option_runs_for_scope(
        account_id=account_id,
        strategy_id=str(strategy_id),
        environment=env,
    )
    reads = _RunReads(session_factory, _run_store(request, session_factory))
    return OptionRunListResponse(
        strategy_id=str(strategy_id),
        coverage=str(coverage.get("coverage") or COVERAGE_UNKNOWN),
        coverage_reason=str(coverage.get("reason") or ""),
        runs=[_option_run_out(row, reads) for row in rows],
    )


@router.get(
    "/{strategy_id}/option-runs/{option_run_id}", response_model=OptionRunDetailResponse
)
async def get_option_run(
    strategy_id: str,
    option_run_id: str,
    request: Request,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_option_runs_db),
):
    """One of the strategy's own option runs, with the evidence around it.

    A run that is not bound to this strategy in its own ``(account,
    environment)`` scope is a 404 - foreign runs are never revealed, not even by
    their existence. A run this strategy DOES own but whose state cannot be read
    is reported as ``coverage: "unknown"`` rather than 404.
    """
    account_id, env = _option_run_scope(repo, owner, strategy_id, environment)
    service = _snapshot_service(request, session_factory)
    rows, _coverage = service.option_runs_for_scope(
        account_id=account_id,
        strategy_id=str(strategy_id),
        environment=env,
    )
    edges = _owned_run_binding(
        session_factory,
        strategy_id=str(strategy_id),
        account_id=account_id,
        environment=env,
        option_run_id=option_run_id,
    )
    row = next(
        (
            item
            for item in rows
            if str(item.get("option_run_id") or "") == str(option_run_id)
        ),
        None,
    )
    if row is None:
        if not edges:
            raise HTTPException(status_code=404, detail="Option run not found")
        row = _degraded_row(option_run_id, edges)

    reads = _RunReads(session_factory, _run_store(request, session_factory))
    return OptionRunDetailResponse(
        run=_option_run_out(row, reads),
        edges=[
            OptionRunEdgeResponse(
                plan_id=str(edge.plan_id),
                phase=str(edge.phase),
                created_at=_iso(edge.created_at),
            )
            for edge in edges
        ],
        frozen=_frozen(row, reads.plans(row)),
        refusals=_refusals(session_factory, strategy_id=str(strategy_id)),
        greeks=_greeks(),
        pnl=_pnl(reads.durable_run(str(option_run_id))),
    )
