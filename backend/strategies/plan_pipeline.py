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


class OptionMarginEvidenceRefusal(Exception):
    """An option margin read that must not collapse into generic unavailable."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})


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


def _live_kite_for_account(account_scope: str, session_factory: Optional[Callable[[], Any]] = None):
    """The authoritative broker client for an owner's live account binding.

    A supplied ``session_factory`` is honoured (the pipeline owns one), and the
    caller-less path falls back to the worker router's own loader. Both read the
    same persisted ``public.kite_sessions`` row and never issue a mutation.
    """
    if session_factory is None:
        from backend.api.routers.worker_shared import _load_live_kite_for_account

        return _load_live_kite_for_account(account_scope)
    from backend.strategies.live_readers import live_kite_for_account

    return live_kite_for_account(account_scope, session_factory=session_factory)


def _leg_margin_required_inr(
    kite: Any,
    account_scope: str,
    plan: Mapping[str, Any],
    legs: Any,
) -> Optional[float]:
    """The broker's own order margin for the plan's exact frozen legs, or unknown.

    The frozen sizing is retained verbatim from the pre-C1.1 reader: a weight is
    a FRACTION of the frozen capital basis (not a share count), and an unsized
    weighted leg or incompletely priced response is unknown evidence, not zero.
    """
    from backend.broker_api.orders.models import OrderMarginInput
    from backend.broker_api.orders.service import OrdersService

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
        # A plan with no increasing leg (a pure reduction) needs no order margin;
        # it still needs FUNDS evidence, so this is zero and not "unknown".
        return 0.0
    quotes = list(
        OrdersService().order_margins(kite, items, f"admission-{account_scope}", None) or []
    )
    if len(quotes) != len(items):
        return None
    if any(getattr(quote, "total", None) is None for quote in quotes):
        return None
    return float(sum(float(getattr(quote, "total", 0.0) or 0.0) for quote in quotes))


def _cnc_available_cash(kite: Any, account_scope: str) -> Optional[float]:
    """``funds.equity.available.cash`` from the read-only portfolio snapshot.

    The CNC cash segment trades against settled cash only, so no other funds
    component is credited. An absent or non-numeric figure is UNKNOWN (``None``),
    never zero: "we did not read it" and "there is none" must not look alike.
    """
    from backend.broker_api.account.portfolio_snapshot import build_portfolio_snapshot

    snapshot = dict(build_portfolio_snapshot(kite, account_scope) or {})
    funds = snapshot.get("funds")
    equity = dict(funds).get("equity") if isinstance(funds, Mapping) else None
    available = dict(equity).get("available") if isinstance(equity, Mapping) else None
    cash = dict(available).get("cash") if isinstance(available, Mapping) else None
    if cash is None or isinstance(cash, bool):
        return None
    try:
        return float(cash)
    except (TypeError, ValueError):
        return None


def live_margin_evidence(
    account_scope: str,
    plan: Mapping[str, Any],
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Authoritative live CNC funding evidence for the plan's legs, or ``None``.

    The ONE evidence reader carries two facts together:

    * ``required_inr`` - the broker's own order margin for the exact frozen legs;
    * ``usable`` - authoritative account FUNDS (``equity.available.cash``) read
      through the read-only portfolio snapshot boundary.

    ``None`` is a real answer: admission refuses MARGIN_UNAVAILABLE rather than
    assuming headroom, which is the fail-closed behaviour D-9 requires. The
    observation instant travels with it so admission can refuse a stale one.

    A PROGRAMMING error (TypeError/AttributeError/NameError) is deliberately not
    caught: a bug in this reader must surface, never masquerade as "no evidence".
    Only a genuine read failure becomes unavailable evidence.
    """
    try:
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        if not legs:
            return None
        kite = _live_kite_for_account(account_scope, session_factory)
        required_inr = _leg_margin_required_inr(kite, account_scope, plan, legs)
        if required_inr is None:
            return None
        usable = _cnc_available_cash(kite, account_scope)
        if usable is None:
            return None
        return {
            "usable": usable,
            "required_inr": required_inr,
            "required_margin_inr": required_inr,
            "as_of": _utcnow(),
            "source": "portfolio_snapshot:funds.equity.available.cash",
            "account_scope": str(account_scope or ""),
            "legs": [
                str(leg.get("instrument_id") or leg.get("tradingsymbol") or "")
                for leg in legs
            ],
        }
    except (TypeError, AttributeError, NameError):
        # A programming error is a bug to surface, not "unavailable evidence".
        raise
    except Exception:  # noqa: BLE001 - a genuine read failure is not headroom
        return None


def _option_margin_items(kite: Any, account_scope: str, legs: list[Any]) -> Optional[list[Any]]:
    from backend.broker_api.orders.models import OrderMarginInput

    items = []
    for leg in legs:
        quantity = abs(float(leg.get("signed_quantity") or 0.0))
        if quantity <= 0:
            continue
        reference_price = float(leg.get("reference_price") or 0.0)
        if reference_price <= 0:
            return None
        items.append(
            OrderMarginInput(
                exchange=str(leg.get("broker_exchange") or leg.get("exchange") or "NFO"),
                tradingsymbol=str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
                transaction_type="BUY" if float(leg.get("signed_quantity") or 0) >= 0 else "SELL",
                variety="regular",
                product=str(leg.get("product") or "NRML"),
                order_type="MARKET",
                quantity=quantity,
                price=reference_price,
            )
        )
    return items


def _basket_required_inr(
    kite: Any,
    account_scope: str,
    items: list[Any],
    *,
    consider_positions: bool = True,
) -> Optional[float]:
    from backend.broker_api.orders.service import OrdersService

    basket = OrdersService().basket_margins(
        kite,
        items,
        consider_positions=consider_positions,
        corr_id=f"admission-{account_scope}",
        mode="compact",
    )
    total = getattr(basket, "final", None)
    if total is None or getattr(total, "total", None) is None:
        return None
    return float(total.total)


def _option_available_funds(kite: Any) -> Optional[float]:
    margins = kite.margins()
    equity = dict(margins).get("equity")
    available = dict(equity).get("available") if isinstance(equity, Mapping) else None
    cash = dict(available).get("cash") if isinstance(available, Mapping) else None
    if cash is None or isinstance(cash, bool):
        return None
    try:
        return float(cash)
    except (TypeError, ValueError):
        return None


def _option_is_reduction_only(plan: Mapping[str, Any], legs: list[Any]) -> bool:
    option_run = dict((plan.get("resolved_plan") or {}).get("option_run") or {})
    return str(option_run.get("phase") or "") == "exit" or all(
        float(leg.get("signed_quantity") or 0.0) <= 0 for leg in legs
    )


def option_live_margin_evidence(
    account_scope: str,
    plan: Mapping[str, Any],
    *,
    session_factory: Optional[Callable[[], Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Authoritative live option basket-margin evidence, fail-closed by name.

    A broker read failure or unusable funds becomes generic unavailable. Roll
    overlap is special: a missing overlap basket is not the final basket, so it
    raises ``LIVE_OPTION_ROLL_PEAK_UNAVAILABLE`` rather than understating risk.
    """
    try:
        resolved = dict(plan.get("resolved_plan") or {})
        legs = list(resolved.get("legs") or [])
        if not legs:
            return None
        kite = _live_kite_for_account(account_scope, session_factory)
        observed_scope = getattr(kite, "account_scope", None)
        if observed_scope is not None and str(observed_scope) != str(account_scope or ""):
            raise OptionMarginEvidenceRefusal(
                "LIVE_OPTION_MARGIN_EVIDENCE_SCOPE_MISMATCH",
                {"expected_account_scope": str(account_scope or ""), "observed": str(observed_scope)},
            )
        items = _option_margin_items(kite, account_scope, legs)
        if items is None:
            return None
        if _option_is_reduction_only(plan, legs):
            required = 0.0
            final_required = 0.0
            peak_required = 0.0
            basis = "basket_final"
        else:
            final_required = _basket_required_inr(kite, account_scope, items)
            if final_required is None:
                return None
            old_legs = list(resolved.get("old_legs") or [])
            option_run = dict(resolved.get("option_run") or {})
            is_roll = str(option_run.get("phase") or "") == "adjust" and bool(old_legs)
            if is_roll:
                old_items = _option_margin_items(kite, account_scope, old_legs)
                if old_items is None:
                    raise OptionMarginEvidenceRefusal(
                        "LIVE_OPTION_ROLL_PEAK_UNAVAILABLE",
                        {"reason": "old_generation_items_unavailable"},
                    )
                peak_required = _basket_required_inr(kite, account_scope, items + old_items)
                if peak_required is None:
                    raise OptionMarginEvidenceRefusal(
                        "LIVE_OPTION_ROLL_PEAK_UNAVAILABLE",
                        {"reason": "overlap_basket_unavailable"},
                    )
                required = max(float(final_required), float(peak_required))
                basis = "roll_peak"
            else:
                peak_required = float(final_required)
                required = float(final_required)
                basis = "basket_final"
        usable = _option_available_funds(kite)
        if usable is None:
            return None
        return {
            "usable": usable,
            "required_inr": required,
            "required_margin_inr": required,
            "margin_basis": basis,
            "as_of": now or _utcnow(),
            "source": "broker_basket_margin",
            "account_scope": str(account_scope or ""),
            "legs": [
                str(leg.get("instrument_id") or leg.get("tradingsymbol") or "")
                for leg in legs
            ],
            "breakdown": {
                "final_required_inr": final_required,
                "peak_required_inr": peak_required,
            },
        }
    except OptionMarginEvidenceRefusal:
        raise
    except (TypeError, AttributeError, NameError):
        raise
    except Exception:
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
        account_scope = str(plan.get("account_id") or "")
        if self._margin_reader is not None:
            try:
                return self._margin_reader(account_scope, plan)
            except OptionMarginEvidenceRefusal as exc:
                raise PipelineRefusal(exc.reason_code, exc.detail) from exc
        if (
            str((plan.get("resolved_plan") or {}).get("target_kind") or "")
            == "option_structure"
        ):
            try:
                return option_live_margin_evidence(
                    account_scope, plan, session_factory=self.session_factory
                )
            except OptionMarginEvidenceRefusal as exc:
                raise PipelineRefusal(exc.reason_code, exc.detail) from exc
        return live_margin_evidence(account_scope, plan, session_factory=self.session_factory)

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
        version_binding: Optional[Mapping[str, Any]] = None,
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
            version_binding=(
                dict(version_binding) if version_binding else None
            ),
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
