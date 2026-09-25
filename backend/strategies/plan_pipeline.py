"""The ONE shared authoritative path from a frozen plan to execution.

Before Phase 2 this logic lived in the operator router: admission, reservation,
approval and execution each had a route that re-implemented its own share of the
sequence (and the reservation route accepted a caller-supplied environment). A
governed execution request must take exactly the same path the operator does, so
the sequence is extracted here and the router becomes a thin caller.

Two invariants are the reason this module exists rather than a second copy:

* **The environment is DERIVED.** ``plan -> proposal envelope.strategy_run_id ->
  strategy_run_bindings`` decides paper/live/dry-run. There is no parameter, in
  this module or in any caller, that can switch it.
* **Nothing here decides *whether* to authorise.** Admission, reservation and
  approval are the same services the operator routes already used; the caller
  (an operator click or the dispatcher acting on a recorded decision) supplies
  the actor, and the recorded evidence travels with it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineRefusal(Exception):
    """A named refusal from the shared pipeline (nothing was executed)."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


def live_margin_evidence(account_scope: str, plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Authoritative live margin for the plan's legs, or ``None``.

    ``None`` is a real answer here: admission refuses MARGIN_UNAVAILABLE rather
    than assuming headroom, which is the fail-closed behaviour D-9 requires. The
    quote's timestamp travels with it so admission can refuse a stale one.
    """
    try:
        from backend.api.routers.worker_shared import _load_live_kite_for_account
        from backend.broker_api.orders.models import OrderMarginInput
        from backend.broker_api.orders.service import OrdersService

        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        if not legs:
            return None
        resolved = dict(plan.get("resolved_plan") or {})
        logical = dict(plan.get("logical_plan") or {})
        try:
            capital_basis = resolved.get("capital_basis_inr", logical.get("capital_basis_inr"))
            capital_basis = None if capital_basis is None else float(capital_basis)
        except (TypeError, ValueError):
            capital_basis = None
        try:
            buffer_pct = resolved.get("cash_buffer_pct", logical.get("cash_buffer_pct"))
            buffer_pct = 0.0 if buffer_pct is None else float(buffer_pct)
        except (TypeError, ValueError):
            buffer_pct = 0.0
        items = []
        for leg in legs:
            # A weight is a FRACTION of the frozen capital basis, not a share
            # count: asking the broker for margin on 0.25 "shares" would
            # under-state the requirement by orders of magnitude.
            if leg.get("signed_quantity") is None and leg.get("target_weight") is not None:
                price = float(leg.get("reference_price") or 0)
                if capital_basis is None or price <= 0:
                    return None
                quantity = (
                    abs(float(leg.get("target_weight") or 0.0))
                    * capital_basis
                    * max(0.0, 1.0 - buffer_pct)
                    / price
                )
                side = "BUY"
            else:
                quantity = abs(float(leg.get("signed_quantity") or 0.0))
                side = "BUY" if float(leg.get("signed_quantity") or 0) >= 0 else "SELL"
            if quantity <= 0:
                continue
            items.append(
                OrderMarginInput(
                    exchange=str(leg.get("broker_exchange") or leg.get("exchange") or "NSE"),
                    tradingsymbol=str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
                    transaction_type=side,
                    variety="regular",
                    product=str(leg.get("product") or "CNC"),
                    order_type="MARKET",
                    quantity=quantity,
                    price=float(leg.get("reference_price") or 0),
                )
            )
        if not items:
            return None
        kite = _load_live_kite_for_account(account_scope)
        quotes = OrdersService().order_margins(kite, items, f"admission-{account_scope}", None)
        usable = sum(float(getattr(quote, "total", 0.0) or 0.0) for quote in quotes)
        return {
            "usable": usable,
            "as_of": datetime.now(timezone.utc),
            "legs": [str(getattr(quote, "tradingsymbol", "") or "") for quote in quotes],
        }
    except Exception:  # noqa: BLE001 - unavailable evidence is not headroom
        return None


class PlanExecutionPipeline:
    """Authoritative plan lookup, admission, reservation, approval, execution.

    Every collaborator is injectable so the pipeline can be exercised against a
    real service with a fake broker boundary; the defaults are the production
    services the operator router already used.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        proposal_store: Any = None,
        admission_service: Any = None,
        reservation_ledger: Any = None,
        approval_service: Any = None,
        margin_reader: Optional[Callable[[str, Mapping[str, Any]], Optional[Dict[str, Any]]]] = None,
        paper_executor_factory: Optional[Callable[[], Any]] = None,
        live_executor_factory: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._proposal_store = proposal_store
        self._admission = admission_service
        self._ledger = reservation_ledger
        self._approvals = approval_service
        self._margin_reader = margin_reader
        self._paper_executor_factory = paper_executor_factory
        self._live_executor_factory = live_executor_factory
        self._clock = clock or _utcnow

    # -- collaborators ------------------------------------------------------

    @property
    def proposals(self) -> Any:
        if self._proposal_store is None:
            from backend.strategies.proposals import ProposalStore

            self._proposal_store = ProposalStore(session_factory=self.session_factory)
        return self._proposal_store

    @property
    def admission(self) -> Any:
        if self._admission is None:
            from backend.strategies.admission import AdmissionService

            self._admission = AdmissionService(session_factory=self.session_factory)
        return self._admission

    @property
    def ledger(self) -> Any:
        if self._ledger is None:
            from backend.strategies.reservations import ReservationLedger

            self._ledger = ReservationLedger(session_factory=self.session_factory)
        return self._ledger

    @property
    def approvals(self) -> Any:
        if self._approvals is None:
            from backend.strategies.approvals import ApprovalService

            self._approvals = ApprovalService(session_factory=self.session_factory)
        return self._approvals

    # -- sequencing ---------------------------------------------------------

    def plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        return self.proposals.get_plan(str(plan_id))

    def environment(self, plan: Mapping[str, Any]) -> str:
        """The plan's environment, from PERSISTED binding authority."""
        from backend.strategies.live_authority import LiveAuthorityRefusal, plan_binding

        try:
            binding = plan_binding(self.session_factory, plan=plan)
        except LiveAuthorityRefusal as exc:
            raise PipelineRefusal(exc.reason_code, exc.detail) from exc
        environment = str(binding.get("execution_environment") or "")
        if environment not in ("paper", "dry_run", "live"):
            raise PipelineRefusal(
                "PLAN_ENVIRONMENT_UNRESOLVED",
                {"plan_id": str(plan.get("plan_id") or ""), "environment": environment},
            )
        return environment

    def binding(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        from backend.strategies.live_authority import LiveAuthorityRefusal, plan_binding

        try:
            return dict(plan_binding(self.session_factory, plan=plan))
        except LiveAuthorityRefusal as exc:
            raise PipelineRefusal(exc.reason_code, exc.detail) from exc

    def margin(self, plan: Mapping[str, Any], environment: str) -> Optional[Dict[str, Any]]:
        if environment != "live":
            return None
        reader = self._margin_reader or live_margin_evidence
        return reader(str(plan.get("account_id") or ""), plan)

    def admit(self, plan: Mapping[str, Any], *, environment: str) -> Dict[str, Any]:
        self._assert_option_structure_admissible(plan, environment=environment)
        verdict = self.admission.evaluate(
            plan,
            execution_environment=environment,
            margin_evidence=self.margin(plan, environment),
        )
        return verdict.as_dict() if hasattr(verdict, "as_dict") else dict(verdict)

    def _assert_option_structure_admissible(
        self, plan: Mapping[str, Any], *, environment: str
    ) -> None:
        """Refuse an option plan the strategy's own durable work already blocks.

        The rules live with the plan/run binding edge, one per frozen phase
        (``assess_option_entry_admissibility`` for an ENTRY,
        ``assess_option_adjust_admissibility`` for an ADJUST), and are the SAME
        ones the execution-time gates apply; asking them BEFORE admission means a
        plan that would be refused at submission is never admitted (or later
        approved) as if it could run. Non-option plans and option EXIT plans are
        untouched.
        """
        from backend.options.execution.plan_binding import (
            PlanBindingRefusal,
            assess_option_adjust_admissibility,
            assess_option_entry_admissibility,
            is_option_adjust_plan,
            is_option_entry_plan,
        )

        if is_option_entry_plan(plan):
            assess = assess_option_entry_admissibility
        elif is_option_adjust_plan(plan):
            assess = assess_option_adjust_admissibility
        else:
            # Every other plan kind and every option EXIT is untouched - answered
            # here so a non-option admission never opens a session for this.
            return
        try:
            with self.session_factory() as session:
                assess(
                    plan,
                    strategy_id=str(plan.get("strategy_id") or ""),
                    account_id=str(plan.get("account_id") or ""),
                    execution_environment=str(environment),
                    session=session,
                )
        except PlanBindingRefusal as exc:
            raise PipelineRefusal(exc.reason_code, exc.detail) from exc

    def reservation_for_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        return self.ledger.for_plan(str(plan_id))

    def reserve(
        self,
        plan: Mapping[str, Any],
        *,
        environment: str,
        actor: str,
        validity_seconds: int = 900,
    ) -> Dict[str, Any]:
        """Admit and claim capacity in one transaction; first claim wins."""
        from datetime import timedelta

        from backend.strategies.reservations import ClaimRequest

        plan_id = str(plan.get("plan_id") or "")
        verdict = self.admit(plan, environment=environment)
        if not bool(verdict.get("admitted")):
            raise PipelineRefusal(
                "ADMISSION_REFUSED",
                {"plan_id": plan_id, "admission": verdict},
            )
        policy = self.admission.policy_for(str(plan.get("strategy_id") or "")) or {}
        admission_detail = dict(verdict.get("detail") or {})
        requirement = float(admission_detail.get("plan_requirement_inr") or 0.0)
        # The account's OWN funds constraint travels with the plan, separately
        # from the strategy's allocation: the ledger enforces both, so two
        # strategies cannot reserve the same actual account funds.
        account_capacity = admission_detail.get("account_available_inr")
        margin = self.margin(plan, environment)
        return self.ledger.claim(
            ClaimRequest(
                plan_id=plan_id,
                strategy_id=str(plan.get("strategy_id") or ""),
                account_id=str(plan.get("account_id") or ""),
                evaluation_id=str(plan.get("evaluation_id") or plan_id),
                execution_environment=environment,
                requirement_inr=requirement,
                valid_until=self._clock() + timedelta(seconds=int(validity_seconds)),
                allocation_inr=policy.get("allocation_inr"),
                account_capacity_inr=(
                    None if account_capacity is None else float(account_capacity)
                ),
                # A staged CNC rebalance funds its increases from its own
                # confirmed reductions, so the ledger defers that part of the
                # account-funds claim instead of refusing it up front.
                staged_increase_inr=(
                    None
                    if admission_detail.get("staged_increase_inr") is None
                    else float(admission_detail["staged_increase_inr"])
                ),
                margin_evidence=margin,
                margin_as_of=(margin or {}).get("as_of"),
                actor_id=str(actor),
            )
        )

    def approve(
        self,
        plan: Mapping[str, Any],
        *,
        actor: str,
        reservation_id: str,
        environment: str,
        actor_kind: str = "manual",
        evidence: Optional[Mapping[str, Any]] = None,
        validity_seconds: Optional[int] = None,
        reuse_existing: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Record the authorisation for the exact plan, or ``None`` if exempt.

        Paper and dry-run are exempt (the existing low-level exemption is
        preserved) — but that exemption is NOT an approval: the caller records
        the user-request decision separately, which is why a paper request
        explicitly configured ``approval_based`` still waits for its owner.
        """
        from backend.strategies.admission import session_product_snapshot
        from backend.strategies.approvals import (
            ApprovalConflict,
            ApprovalNotRequired,
            ApprovalRequest,
            DEFAULT_APPROVAL_VALIDITY_SECONDS,
        )

        plan_id = str(plan.get("plan_id") or "")
        products = [
            str(leg.get("product") or "")
            for leg in (plan.get("resolved_plan") or {}).get("legs") or []
            if leg.get("product")
        ]
        if environment != "live":
            return None
        request = ApprovalRequest(
            plan=dict(plan),
            actor_id=str(actor),
            reservation_id=str(reservation_id),
            execution_environment=environment,
            validity_seconds=int(validity_seconds or DEFAULT_APPROVAL_VALIDITY_SECONDS),
            session_product_snapshot=session_product_snapshot(products),
        )
        try:
            return self.approvals.approve(request, actor_kind=actor_kind, evidence=evidence)
        except ApprovalConflict:
            if not reuse_existing:
                raise
            existing = self.approvals.active_for_plan(plan_id)
            if existing is None:
                raise
            return existing
        except ApprovalNotRequired:
            return None

    # -- execution ----------------------------------------------------------

    def _executor(self, environment: str) -> Any:
        if environment == "live":
            if self._live_executor_factory is None:
                raise PipelineRefusal(
                    "LIVE_EXECUTOR_UNAVAILABLE",
                    {"environment": environment},
                )
            return self._live_executor_factory()
        if self._paper_executor_factory is None:
            raise PipelineRefusal("PAPER_EXECUTOR_UNAVAILABLE", {"environment": environment})
        return self._paper_executor_factory()

    async def execute(self, plan: Mapping[str, Any], *, actor: str) -> Dict[str, Any]:
        """Execute one admitted plan through the environment's own executor."""
        from backend.strategies.execution import ExecutionRefusal
        from backend.strategies.live_authority import LiveAuthorityRefusal

        environment = self.environment(plan)
        executor = self._executor(environment)
        try:
            result = await executor.execute(plan, actor=str(actor))
        except ExecutionRefusal as exc:
            raise PipelineRefusal(exc.reason_code, exc.detail) from exc
        except LiveAuthorityRefusal as exc:
            raise PipelineRefusal(exc.reason_code, exc.detail) from exc
        return dict(result)
