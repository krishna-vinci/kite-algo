"""Production factory and executor for hosted LIVE plan execution (Phase 1).

This is the shared live path: one service factory wires the existing intent
handler, canonical attribution, account truth, admission, reservations,
approvals, the execution barrier and the durable submission claim. There is no
independent live fill ledger and no second options engine.

Phase 1 dispatches ``single_instrument`` plans only. Every other plan kind is a
named refusal (``LIVE_PLAN_KIND_UNSUPPORTED``); the all-lane dispatch is the next
phase and reuses this same executor/factory.

The deployment setting ``HOSTED_LIVE_ENABLED`` gates ALL live execution here and
is false by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import text

from backend.app.database import SessionLocal
from backend.strategies.execution import ExecutionRefusal, PaperPlanExecutor
from backend.strategies.live_adapter import LivePlanAdapter, LiveRefusal
from backend.strategies.live_authority import LiveAuthorityRefusal, derive_live_authority, live_authority_reader
from backend.strategies.live_readers import (
    LiveEvidenceUnavailable,
    attributed_position_reader,
    ingested_fill_reader,
    live_quote_for_leg,
    live_session_id_for_account,
)
from backend.strategies.plan_pipeline import live_margin_evidence
from backend.strategies.live_settings import hosted_live_disabled_detail, hosted_live_enabled
from backend.strategies.live_sequence import (
    BLOCKER_HEDGE_NOT_FILLED,
    BLOCKER_HEDGE_SHORT_NOT_CLOSED,
    RULE_MIS_SQUAREOFF,
    RULE_HEDGE_FILL_GATE,
    RULE_HEDGE_RELEASE_WITHHELD,
    RULE_ROLL_CLOSE_RELEASED,
    RULE_STAGED_FUNDING_GATE,
    LivePlanSequence,
    lane_for_plan,
    prerequisites_met,
)
from backend.strategies.reservations import ReservationLedger
from backend.strategies.settlement import ExecutionBarrier

LIVE_ENVIRONMENT = "live"

#: The exchange-local zone every platform square-off schedule is expressed in.
#: ``mis_squareoff.squareoff_schedule()`` returns exchange-local wall-clock times
#: (15:20 NSE/MCX 23:20), so comparing them against a UTC "now" would read the
#: due time 5h30m late - which is exactly a guessed close.
EXCHANGE_TZ = timezone(timedelta(hours=5, minutes=30))

#: Plan kinds this executor dispatches. Every lane runs on the SAME executor,
#: parent protocol and release pass; the lane builders live in ``live_sequence``.
LIVE_PLAN_KINDS = (
    "single_instrument",
    "target_weights",
    "target_futures",
    "option_structure",
)

EXECUTABLE_RESERVATION_STATUSES = ("active", "renewed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def build_live_plan_adapter(
    session_factory: Optional[Callable[[], Any]] = None,
    *,
    intent_handler: Any = None,
    fill_reader: Any = None,
    position_reader: Any = None,
    authority_reader: Any = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> LivePlanAdapter:
    """The production live adapter: every reader comes from real platform data.

    ``intent_handler`` is the broker boundary. Tests inject a FAKE broker there;
    production leaves it unset and the factory builds the real intent handler, so
    a test can never accidentally become the production wiring.
    """
    factory = session_factory or SessionLocal
    if intent_handler is None:
        from backend.algo_runtime.intent_bridge import KiteOrdersIntentHandler

        intent_handler = KiteOrdersIntentHandler(session_factory=factory)
    return LivePlanAdapter(
        session_factory=factory,
        barrier=ExecutionBarrier(session_factory=factory),
        intent_handler=intent_handler,
        fill_reader=fill_reader or ingested_fill_reader(factory),
        position_reader=position_reader or attributed_position_reader(factory),
        authority_reader=authority_reader or live_authority_reader(factory),
        clock=clock or _utcnow,
    )


class LivePlanExecutor:
    """Execute one frozen plan on the live book, or refuse by name."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        adapter: Optional[LivePlanAdapter] = None,
        ledger: Optional[ReservationLedger] = None,
        clock: Optional[Callable[[], datetime]] = None,
        intent_handler: Any = None,
        environ: Optional[Mapping[str, str]] = None,
        quote_reader: Any = None,
        margin_reader: Any = None,
        session_id_reader: Any = None,
        mis_clock: Optional[Callable[[], datetime]] = None,
        authorization: Any = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self.adapter = adapter
        self.ledger = ledger or ReservationLedger(session_factory=self.session_factory)
        self._clock = clock or _utcnow
        self._intent_handler = intent_handler
        self._environ = environ
        # Market/margin evidence are the broker/network boundary; production
        # leaves these unset so the real wrappers are used. A test may inject
        # deterministic market data, never an authority or an admission verdict.
        self._quote_reader = quote_reader
        self._margin_reader = margin_reader
        self._session_id_reader = session_id_reader
        #: The clock the PLATFORM SESSION compares a square-off schedule against.
        #: Production leaves it equal to the executor's clock; it is separate so a
        #: deployment (or a test) can pin the session instant while authority,
        #: quote freshness and reservation windows keep using the request clock.
        self._session_clock = mis_clock or self._clock
        #: Phase 2: the governed-execution authorization reader. It re-derives the
        #: authority of the GOVERNING request - mode, grant, version, source,
        #: policy and attempt - on every dependent release, so the initial request
        #: cannot hand unbounded approval to a step that is released later. When a
        #: caller injects nothing, the authoritative reader is constructed here:
        #: the production release pass must never run un-governed by omission.
        if authorization is None:
            from backend.strategies.execution_requests import ExecutionRequestService

            authorization = ExecutionRequestService(session_factory=self.session_factory)
        self._authorization = authorization
        self._trail = PaperPlanExecutor(session_factory=self.session_factory, clock=self._clock)
        #: The durable multi-step parent store, shared by first-leg dispatch and
        #: the sequence release pass so both read the SAME frozen protocol.
        self.sequence = LivePlanSequence(
            session_factory=self.session_factory,
            ledger=self.ledger,
            clock=self._clock,
        )
        # The option domain contributes its settlement axes to the execution
        # barrier (a run with no authoritative settlement evidence is ``unsettled``,
        # never quietly settled). Registering here - idempotently - is what makes
        # that adapter ACTUALLY wired on the live path rather than merely available.
        try:
            from backend.options.protection.expiry_policy import (
                register_option_settlement_adapter,
            )

            register_option_settlement_adapter()
        except ImportError:  # pragma: no cover - a deployment without the option engine
            pass

    # ------------------------------------------------------------------ entry

    async def execute(self, plan: Mapping[str, Any], *, actor: str) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        plan_kind = str(plan.get("plan_kind") or "")
        reservation = self.ledger.for_plan(plan_id)
        try:
            if not hosted_live_enabled(self._environ):
                raise ExecutionRefusal(
                    "LIVE_DISABLED",
                    hosted_live_disabled_detail(plan_id=plan_id, surface="plan_execute"),
                )
            if plan_kind not in LIVE_PLAN_KINDS:
                raise ExecutionRefusal(
                    "LIVE_PLAN_KIND_UNSUPPORTED",
                    {"plan_id": plan_id, "plan_kind": plan_kind, "supported": list(LIVE_PLAN_KINDS)},
                )
            envelope = self._envelope(plan)
            if str(envelope.get("status") or "") != "validated":
                raise ExecutionRefusal(
                    "PLAN_NOT_VALIDATED",
                    {"plan_id": plan_id, "proposal_status": str(envelope.get("status") or "")},
                )
            # Environment/authority come from PERSISTED records, never the
            # request or the reservation alone.
            derived = derive_live_authority(self.session_factory, plan=plan, now=self._clock())
            binding = derived["binding"]
            authority = derived["authority"]
            legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
            if not legs:
                raise ExecutionRefusal(
                    "LIVE_PLAN_COMPOSITION_EMPTY", {"plan_id": plan_id}
                )
            lane = lane_for_plan(plan)
            increasing = any(self._increases_exposure(dict(leg)) for leg in legs)
            if increasing or reservation is not None:
                self._reservation_preconditions(reservation)
        except ExecutionRefusal as exc:
            self._record_refusal(plan_id=plan_id, actor=actor, exc=exc)
            raise
        except LiveAuthorityRefusal as exc:
            wrapped = ExecutionRefusal(exc.reason_code, exc.detail)
            self._record_refusal(plan_id=plan_id, actor=actor, exc=wrapped)
            raise wrapped from exc
        except LiveRefusal as exc:
            wrapped = ExecutionRefusal(exc.reason_code, exc.detail)
            self._record_refusal(plan_id=plan_id, actor=actor, exc=wrapped)
            raise wrapped from exc

        account_id = str(plan.get("account_id") or "")
        try:
            margin_reader = self._margin_reader or (
                lambda account, plan: live_margin_evidence(account, plan, session_factory=self.session_factory)
            )
            margin = margin_reader(account_id, plan)
            session_id_reader = self._session_id_reader or (
                lambda account: live_session_id_for_account(account, session_factory=self.session_factory)
            )
            session_id = session_id_reader(account_id)
            quote_reader = self._quote_reader or live_quote_for_leg
            quote = None
            if len(legs) == 1:
                # A one-leg plan's quote is read up front and validated as before;
                # a multi-leg parent reads a FRESH quote per leg at dispatch.
                quote = quote_reader(legs[0])
                if hasattr(quote, "__await__"):
                    quote = await quote
            adapter = self.adapter or build_live_plan_adapter(
                self.session_factory, intent_handler=self._intent_handler, clock=self._clock
            )
            submission = await adapter.submit(
                plan,
                actor=actor,
                run_binding=binding,
                evaluation_authority=authority,
                quote=quote,
                margin_evidence=margin,
                session_id=session_id,
                lane=lane,
                sequence=self.sequence,
                quote_reader=quote_reader,
            )
        except ExecutionRefusal as exc:
            self._record_refusal(plan_id=plan_id, actor=actor, exc=exc)
            raise
        except LiveAuthorityRefusal as exc:
            wrapped = ExecutionRefusal(exc.reason_code, exc.detail)
            self._record_refusal(plan_id=plan_id, actor=actor, exc=wrapped)
            raise wrapped from exc
        except LiveEvidenceUnavailable as exc:
            wrapped = ExecutionRefusal(exc.reason_code, exc.detail)
            self._record_refusal(plan_id=plan_id, actor=actor, exc=wrapped)
            raise wrapped from exc
        except LiveRefusal as exc:
            wrapped = ExecutionRefusal(exc.reason_code, exc.detail)
            self._record_refusal(plan_id=plan_id, actor=actor, exc=wrapped)
            raise wrapped from exc

        return self._record_submission(plan=plan, actor=actor, submission=submission, reservation=reservation)

    # ------------------------------------------------------------- internals

    def _envelope(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        from backend.strategies.proposals import ProposalStore

        envelope = ProposalStore(session_factory=self.session_factory).get_proposal(
            str(plan.get("proposal_id") or "")
        )
        if envelope is None:
            raise ExecutionRefusal(
                "PLAN_NOT_VALIDATED",
                {"plan_id": str(plan.get("plan_id") or ""), "message": "no proposal envelope for this plan"},
            )
        return envelope

    @staticmethod
    def _increases_exposure(leg: Mapping[str, Any]) -> bool:
        current = int(leg.get("_current_quantity") or 0)
        target_raw = leg.get("signed_quantity")
        if target_raw is None:
            return True
        target = int(target_raw)
        if target == 0:
            return False
        if current == 0 or current * target > 0:
            return abs(target) > abs(current)
        # Crossing or leaving flat always increases the book on one side.
        return True

    def _reservation_preconditions(self, reservation: Optional[Dict[str, Any]]) -> None:
        plan_id = str(reservation.get("plan_id") or "") if reservation else ""
        if reservation is None or str(reservation.get("status")) not in EXECUTABLE_RESERVATION_STATUSES:
            raise ExecutionRefusal(
                "RESERVATION_REQUIRED",
                {
                    "plan_id": plan_id,
                    "reservation_status": None if reservation is None else str(reservation.get("status")),
                },
            )
        environment = str(reservation.get("execution_environment") or "")
        if environment != LIVE_ENVIRONMENT:
            raise ExecutionRefusal(
                "PAPER_ONLY_EXECUTION",
                {
                    "plan_id": plan_id,
                    "execution_environment": environment,
                    "message": "this executor serves live accounts only",
                },
            )
        valid_until = _as_datetime(reservation.get("valid_until"))
        if valid_until is not None and self._clock() >= valid_until:
            raise ExecutionRefusal("RESERVATION_EXPIRED", {"plan_id": plan_id})

    def _record_refusal(self, *, plan_id: str, actor: str, exc: ExecutionRefusal) -> None:
        self._trail._record_event(
            plan_id,
            step_no=1,
            event="rejected",
            refusal_reason=exc.reason_code,
            actor_id=actor,
            detail=exc.detail,
            at=self._clock(),
        )

    def _record_submission(
        self,
        *,
        plan: Mapping[str, Any],
        actor: str,
        submission: Any,
        reservation: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Write the per-step trail for a materialized plan, then report it.

        A ``withheld`` step is deliberately NOT written as a submission: nothing
        was sent, and the durable protocol (the parent row, its claim and the
        barrier ``work_created``) is the record. The release pass appends the
        ``submitted`` event when - and only when - it actually releases the step.
        """
        plan_id = str(plan.get("plan_id") or "")
        entries = list(getattr(submission, "steps", None) or [])
        if not entries:
            state = str(getattr(submission, "state", "") or "")
            entries = [
                {
                    "step_no": 1,
                    "step_ref": str(getattr(submission, "step_ref", "") or ""),
                    "state": state,
                    "event": {
                        "no_op": "no_op",
                        "rejected": "rejected",
                        "filled": "filled",
                    }.get(state, "submitted"),
                    "refusal_reason": getattr(submission, "reason_code", None),
                    "broker_order_ids": list(getattr(submission, "broker_order_ids", []) or []),
                    "detail": dict(getattr(submission, "detail", {}) or {}),
                    "withheld": False,
                }
            ]

        steps_out: List[Dict[str, Any]] = []
        broker_order_ids: List[str] = []
        status = "submitted"
        for entry in entries:
            state = str(entry.get("state") or "")
            event = str(entry.get("event") or "submitted")
            refusal_reason = entry.get("refusal_reason")
            detail = dict(entry.get("detail") or {})
            step_orders = [str(value) for value in (entry.get("broker_order_ids") or [])]
            if state == "uncertain":
                detail = {**detail, "state": "uncertain", "recovery_required": True}
            elif state in ("pending", "releasing"):
                detail = {**detail, "state": state}
            if not entry.get("withheld"):
                self._trail._record_event(
                    plan_id,
                    step_no=int(entry.get("step_no") or 0),
                    event=event,
                    refusal_reason=refusal_reason,
                    actor_id=actor,
                    detail=detail,
                    broker_order_id=(step_orders[0] if step_orders else None),
                    at=self._clock(),
                )
            broker_order_ids.extend(step_orders)
            steps_out.append(
                {
                    "step_no": int(entry.get("step_no") or 0),
                    "state": state,
                    "event": event,
                    "refusal_reason": refusal_reason,
                    "withheld": bool(entry.get("withheld")),
                    "depends_on": [int(value) for value in (entry.get("depends_on") or [])],
                    "detail": detail,
                    "broker_order_ids": step_orders,
                }
            )
            if state == "rejected":
                status = "rejected"

        terminal_states = {str(entry.get("state") or "") for entry in entries}
        if terminal_states == {"rejected"}:
            # EVERY leg was authoritatively refused: the reservation's capacity
            # backs nothing, so it is released once. One refused leg among legs
            # that are still pending keeps the reservation held for them.
            self._release_terminal_unfilled(reservation, reason="broker_rejected")

        # A terminal step that the executor itself resolved (an authoritative
        # refusal, or a risk-reducing no-op) also advances the parent: the outcome
        # consumer only sees the states it scans, so "rejected" would otherwise
        # never reach the parent's settlement rule.
        for entry in entries:
            if str(entry.get("state") or "") not in ("rejected", "no_op"):
                continue
            self.sequence.declare_leg_terminal(
                plan_id=plan_id,
                step_no=int(entry.get("step_no") or 0),
                outcome=str(entry.get("state") or ""),
                filled=0,
                ordered=int((entry.get("detail") or {}).get("quantity") or 0),
            )

        return {
            "plan_id": plan_id,
            "status": status,
            "steps": steps_out,
            "reservation_id": str((reservation or {}).get("reservation_id") or "") or None,
            "paper_order_ids": [],
            "broker_order_ids": broker_order_ids,
        }

    def _release_terminal_unfilled(self, reservation: Optional[Dict[str, Any]], *, reason: str) -> None:
        reservation_id = str((reservation or {}).get("reservation_id") or "")
        if not reservation_id:
            return
        try:
            self.ledger.release(
                reservation_id,
                actor_id="live-executor",
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - a refused release is reported by the trail
            pass

    # ------------------------------------------------ shared sequence pass

    async def release_sequence(
        self, *, limit: int = 50, actor: str = "live-sequence"
    ) -> Dict[str, int]:
        """The shared background sequence pass: release withheld steps on evidence.

        A withheld step is dispatched ONLY when, at the moment of the pass, every
        prerequisite is ``filled``; the deployment flag is on; the PERSISTED
        evaluation authority still re-derives (run open + live, token active with
        ``intents:submit``, hosting job in an authority status with a live lease at
        the current attempt); the owner approval still validates against the frozen
        plan; the reservation still validates and covers every outstanding
        increasing leg; and the lane's own release rule (the MIS square-off clock)
        is satisfied. Anything else records a NAMED blocker and places NOTHING.

        It is deliberately not a "continue the plan" button: a restarted process, a
        repeated callback or an expired attempt can never turn into a new order.
        """
        counts = {"scanned": 0, "released": 0, "blocked": 0, "no_op": 0, "errors": 0}
        if not hosted_live_enabled(self._environ):
            return counts
        from backend.strategies.proposals import ProposalStore

        store = ProposalStore(session_factory=self.session_factory)
        try:
            parents = self.sequence.releasable_parents(limit=limit)
        except Exception:  # noqa: BLE001 - an unreadable source is not "nothing to do"
            counts["errors"] += 1
            return counts
        for parent in parents:
            counts["scanned"] += 1
            try:
                plan = store.get_plan(str(parent["plan_id"]))
            except Exception:  # noqa: BLE001 - one bad parent never kills the pass
                counts["errors"] += 1
                continue
            if plan is None:
                continue
            try:
                await self._release_parent(parent, plan, counts=counts, actor=actor)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                counts["errors"] += 1
                # Named, never swallowed: a pass that raised is a degraded pass and
                # an operator has to be able to see which plan it was.
                counts["error_detail"] = f"{parent.get('plan_id')}: {exc}"
            try:
                # A parent whose legs are ALL terminal but whose reservation was
                # never settled (a crashed disposition, a consumer that died before
                # its effect) is repaired here: the parent's own idempotent rule
                # re-attempts the settlement it never recorded.
                self.sequence.settle_parent_if_complete(plan_id=str(parent["plan_id"]))
            except Exception:  # noqa: BLE001 - retried on the next pass
                counts["errors"] += 1
        return counts

    def _release_authority_check(self, plan: Mapping[str, Any]):
        """The governed authority check for one plan's dependent release, or None.

        Returned as a callable so the live adapter runs it INSIDE the release
        claim transaction (holding the canonical book lock AND the hosted-strategy
        row lock), which is what linearises it against a grant revocation.
        """
        authorization = self._authorization
        if authorization is None:
            # No explicit collaborator was injected. Rather than release WITHOUT
            # a governed check, use the PRODUCTION one built from this executor's
            # own session factory: a plan that a governed execution request
            # created is re-derived against that request, and a plan with no
            # governing request (the operator path, a pre-Phase-2 row) is
            # unaffected. A default factory therefore fails closed instead of
            # skipping the authority check.
            from backend.strategies.execution_requests import ExecutionRequestService

            authorization = ExecutionRequestService(self.session_factory)
            self._authorization = authorization
        if hasattr(authorization, "release_authority_check"):
            return authorization.release_authority_check(plan)
        if hasattr(authorization, "authorize_dependent_release"):
            # Compatibility seam: a collaborator that only implements the
            # standalone query is still authoritative, just not session-bound.
            capture = dict(plan)

            def _check(_session: Any) -> Any:
                return authorization.authorize_dependent_release(capture)

            return _check
        return None

    async def _release_parent(
        self,
        parent: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        counts: Dict[str, int],
        actor: str,
    ) -> None:
        plan_id = str(parent["plan_id"])
        specs = {int(spec.step_no): spec for spec in parent["step_spec"]}
        states = self.sequence.step_states(plan_id)
        for step in self.sequence.withheld_steps(plan_id):
            step_no = int(step["step_no"])
            spec = specs.get(step_no)
            if spec is None:
                continue
            if not prerequisites_met(spec, states):
                # Ordinary sequencing: the legs this one depends on have not all
                # filled. Nothing is released and no blocker is recorded, because
                # waiting is the protocol working, not a refusal.
                continue

            # The authority must re-derive from PERSISTED records on every pass:
            # an expired attempt, a revoked token or a moved lease epoch refuses.
            try:
                derived = derive_live_authority(
                    self.session_factory, plan=plan, now=self._clock()
                )
            except LiveAuthorityRefusal as exc:
                self.sequence.record_release_blocker(
                    plan_id=plan_id,
                    step_no=step_no,
                    reason_code=exc.reason_code,
                    detail=exc.detail,
                )
                counts["blocked"] += 1
                continue
            binding = derived["binding"]
            authority = derived["authority"]

            # Phase 2: a GOVERNED plan's dependent step re-derives the authority of
            # the request that started it - its mode, grant, version, source,
            # policy and attempt - against the CURRENT persisted records. The
            # authoritative check also runs INSIDE the release claim transaction
            # (below), so this pass is a fast, named blocker rather than the only
            # gate. Approval-based plans keep their existing pin-by-pin approval
            # check inside the adapter.
            release_check = self._release_authority_check(plan)
            if release_check is not None:
                release_refusal = release_check(None)
                if release_refusal is not None:
                    self.sequence.record_release_blocker(
                        plan_id=plan_id,
                        step_no=step_no,
                        reason_code=str(release_refusal.get("reason_code") or "GRANT_REQUIRED"),
                        detail=dict(release_refusal) | {"stage": "dependent_release"},
                    )
                    counts["blocked"] += 1
                    continue

            allowed, rule_reason, rule_detail = self._lane_release_rule(
                plan=plan,
                spec=spec,
                binding=binding,
                authority=authority,
                parent=parent,
            )
            if not allowed:
                self.sequence.record_release_blocker(
                    plan_id=plan_id,
                    step_no=step_no,
                    reason_code=rule_reason,
                    detail=rule_detail,
                )
                counts["blocked"] += 1
                continue

            account_id = str(plan.get("account_id") or "")
            try:
                margin_reader = self._margin_reader or (
                    lambda account, plan: live_margin_evidence(
                        account, plan, session_factory=self.session_factory
                    )
                )
                session_id_reader = self._session_id_reader or (
                    lambda account: live_session_id_for_account(
                        account, session_factory=self.session_factory
                    )
                )
                adapter = self.adapter or build_live_plan_adapter(
                    self.session_factory,
                    intent_handler=self._intent_handler,
                    clock=self._clock,
                )
                staged_gate = str(spec.release_rule) == RULE_STAGED_FUNDING_GATE
                result = await adapter.release_step(
                    plan,
                    spec,
                    binding=binding,
                    authority=authority,
                    actor=actor,
                    margin_evidence=None if staged_gate else margin_reader(account_id, plan),
                    funds_reader=(lambda: margin_reader(account_id, plan))
                    if staged_gate
                    else None,
                    session_id=session_id_reader(account_id),
                    quote_reader=self._quote_reader or live_quote_for_leg,
                    all_specs=list(parent["step_spec"]),
                    parent=parent,
                    governed_authority_check=release_check,
                )
            except (
                LiveRefusal,
                ExecutionRefusal,
                LiveAuthorityRefusal,
                LiveEvidenceUnavailable,
            ) as exc:
                self.sequence.record_release_blocker(
                    plan_id=plan_id,
                    step_no=step_no,
                    reason_code=str(getattr(exc, "reason_code", "LIVE_SEQUENCE_RELEASE_REFUSED")),
                    detail=dict(getattr(exc, "detail", {}) or {})
                    | {"lane_rule": rule_detail},
                )
                counts["blocked"] += 1
                continue
            if result.get("skipped"):
                continue
            released_state = str(result.get("state") or "")
            if released_state == "no_op":
                counts["no_op"] += 1
            else:
                counts["released"] += 1
            self._record_release_trail(
                plan_id=plan_id, spec=spec, actor=actor, released_state=released_state
            )
            if str(spec.release_rule) == RULE_MIS_SQUAREOFF:
                self._record_mis_squareoff_evidence(
                    plan=plan,
                    spec=spec,
                    parent=parent,
                    authority=authority,
                    released_state=released_state,
                    rule_detail=rule_detail,
                )

    def _lane_release_rule(
        self,
        *,
        plan: Mapping[str, Any],
        spec: Any,
        binding: Mapping[str, Any],
        authority: Mapping[str, Any],
        parent: Optional[Mapping[str, Any]] = None,
    ) -> tuple[bool, str, Dict[str, Any]]:
        """The lane's own release rule. Portfolio/roll dependencies live in
        ``depends_on``; MIS additionally owns the platform's square-off clock, a
        roll close owns its replacement-fill proof and a structure hedge owns its
        proven short closure."""
        rule = str(getattr(spec, "release_rule", ""))
        if rule == RULE_STAGED_FUNDING_GATE:
            # The lane passes sequencing here; executable quote/funds/admission
            # and the keyed reservation authorization run inside the release
            # transaction, immediately before its withheld -> releasing CAS.
            return True, "", {"staged_funding_gate": "enforced_in_release_transaction"}
        if rule == RULE_ROLL_CLOSE_RELEASED:
            return self._roll_close_release_rule(plan=plan, spec=spec)
        if rule == RULE_HEDGE_FILL_GATE:
            return self._hedge_fill_release_rule(spec=spec, parent=parent)
        if rule == RULE_HEDGE_RELEASE_WITHHELD:
            return self._hedge_release_withheld_rule(spec=spec, parent=parent)
        if rule != RULE_MIS_SQUAREOFF:
            return True, "", {}
        from backend.strategies.mis_squareoff import scheduled_time_for, squareoff_schedule
        from backend.strategies.mis_stale_exit import (
            MisStaleExitPolicy,
            STALE_EXIT_POLICY,
        )

        _ = squareoff_schedule()
        mis = dict((getattr(spec, "detail", {}) or {}).get("mis") or {})
        exchange = str(mis.get("exchange") or getattr(spec, "exchange", "") or "")
        now = self._session_clock()
        scheduled = scheduled_time_for(exchange, "MIS")
        detail: Dict[str, Any] = {
            "exchange": exchange,
            "product": "MIS",
            "scheduled_at": scheduled,
            "session_date": now.date().isoformat(),
            "evaluated_at": now.isoformat(),
            "attempt": int(authority.get("attempt") or 0),
        }
        if scheduled:
            hour, minute = (int(part) for part in str(scheduled).split(":")[:2])
            # The schedule is EXCHANGE-LOCAL wall clock: compare it in that zone,
            # never against the UTC instant, or 15:20 reads as 20:50 IST.
            local_now = now.astimezone(EXCHANGE_TZ)
            due = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            detail["due_at"] = due.isoformat()
            if now >= due:
                detail["authority_source"] = "squareoff_clock"
                return True, "", detail

        # Risk reduction is ALWAYS available; it just has to come from the
        # platform's own conditions rather than a guessed exchange close.
        context = self._mis_context(plan=plan, binding=binding)
        if context.get("stop_requested"):
            detail["authority_source"] = "operator_stop"
            return True, "", detail
        if (
            context.get("stale_exit_armed")
            and MisStaleExitPolicy.is_stale(
                last_heartbeat_at=context.get("last_heartbeat_at"),
                stale_seconds=context.get("worker_stale_sec"),
                now=now,
            )
        ):
            detail["authority_source"] = "stale_worker_exit"
            detail["stale_exit_policy"] = STALE_EXIT_POLICY
            return True, "", detail
        return False, "MIS_SQUAREOFF_NOT_DUE", detail

    def _roll_close_release_rule(
        self, *, plan: Mapping[str, Any], spec: Any
    ) -> tuple[bool, str, Dict[str, Any]]:
        """A roll's old-contract close: released ONLY by the roll's own state.

        The rule re-runs the paper executor's ``_roll_preconditions`` against the
        roll as it is NOW, so the release cannot happen while the acquisition is
        partial, stalled, rejected or unknown - ``RollStateMachine`` reaches
        ``releasing_old`` only on the FULL required replacement quantity being
        proven filled by this roll's OWN recorded replacement executions. The
        plan's own contract, account and quantity are re-validated in the same
        call, so a close can never be aimed at another roll, another account or a
        quantity the acquisition did not buy.
        """
        from backend.strategies.execution import ExecutionRefusal, PaperPlanExecutor
        from backend.strategies.live_sequence import BLOCKER_ROLL_CLOSE_NOT_RELEASED

        plan_id = str(plan.get("plan_id") or "")
        trail = PaperPlanExecutor(session_factory=self.session_factory, clock=self._clock)
        try:
            ref = PaperPlanExecutor._roll_binding(plan)
        except ExecutionRefusal as exc:
            return False, str(exc.reason_code), dict(exc.detail)
        role = str((ref or {}).get("role") or "")
        if not ref or role != "close_old":
            return False, BLOCKER_ROLL_CLOSE_NOT_RELEASED, {
                "plan_id": plan_id,
                "step_no": int(getattr(spec, "step_no", 0) or 0),
                "roll_role": role or None,
                "message": (
                    "a roll close step must be the roll's declared close_old half; "
                    "an unbound plan may not close an open roll's old contract"
                ),
            }
        try:
            roll = trail._roll_preconditions(plan, ref)
        except ExecutionRefusal as exc:
            return False, str(exc.reason_code), {
                **dict(exc.detail),
                "plan_id": plan_id,
                "step_no": int(getattr(spec, "step_no", 0) or 0),
                "roll_role": role,
            }
        return True, "", {
            "roll_id": str(roll.get("roll_id") or ""),
            "roll_state": str(roll.get("state") or ""),
            "roll_role": role,
            "required_replacement_quantity": int(
                roll.get("required_replacement_quantity") or 0
            ),
            "proven_filled_quantity": int(roll.get("proven_filled_quantity") or 0),
            "release_authority": "roll_state_machine",
        }

    @staticmethod
    def _parent_leg_outcomes(
        parent: Optional[Mapping[str, Any]],
    ) -> tuple[Dict[int, Any], Dict[int, Dict[str, Any]]]:
        specs = {
            int(item.step_no): item for item in ((parent or {}).get("step_spec") or [])
        }
        legs = dict(((parent or {}).get("detail") or {}).get("legs") or {})
        return specs, {int(key): dict(value or {}) for key, value in legs.items()}

    def _hedge_fill_release_rule(
        self, *, spec: Any, parent: Optional[Mapping[str, Any]]
    ) -> tuple[bool, str, Dict[str, Any]]:
        """A short entry leg: released only against the CONFIRMED hedge fill.

        ``hedge_fill_gate`` is the options engine's own rule and this is a
        production caller of it: the gate is asked how much of the dependent short
        the hedge's CONFIRMED fill releases, and only a FULL release proceeds. A
        submitted-but-unfilled hedge, a partial fill, a rejection, a cancellation
        or a timeout releases nothing and the short stays withheld.
        """
        from backend.options.protection.hedge_gate import hedge_fill_gate

        specs, legs = self._parent_leg_outcomes(parent)
        required = 0
        filled = 0
        outcomes: List[str] = []
        for step_no in tuple(getattr(spec, "depends_on", ()) or ()):
            hedge_spec = specs.get(int(step_no))
            if hedge_spec is None:
                continue
            entry = dict(legs.get(int(step_no)) or {})
            required += abs(int(getattr(hedge_spec, "quantity", 0) or 0))
            filled += int(entry.get("filled_quantity") or 0)
            outcomes.append(str(entry.get("outcome") or "pending"))
        dependent = abs(int(getattr(spec, "quantity", 0) or 0))
        if outcomes and all(value == "filled" for value in outcomes):
            outcome = "filled"
        elif filled > 0:
            outcome = "partially_filled"
        else:
            outcome = "pending"
        decision = hedge_fill_gate(
            required_hedge_quantity=required,
            confirmed_filled_quantity=filled,
            dependent_short_quantity=dependent,
            outcome=outcome,
        )
        detail = {
            "rule": RULE_HEDGE_FILL_GATE,
            "step_no": int(getattr(spec, "step_no", 0) or 0),
            "depends_on": [int(value) for value in (getattr(spec, "depends_on", ()) or ())],
            "hedge_outcomes": outcomes,
            "dependent_short_quantity": dependent,
            **decision.as_dict(),
        }
        if int(decision.released_quantity) < dependent:
            return False, BLOCKER_HEDGE_NOT_FILLED, detail
        return True, "", detail

    def _hedge_release_withheld_rule(
        self, *, spec: Any, parent: Optional[Mapping[str, Any]]
    ) -> tuple[bool, str, Dict[str, Any]]:
        """An exit leg that releases a HEDGE: released only against proven closure.

        The options engine's ``build_structure_exit_orders`` states the rule (short
        liabilities close first, and a hedge is released only for the quantity its
        short is PROVEN to have closed), so this asks the builder directly with the
        parent's own confirmed fills as the proof. Nothing here trusts a submitted
        order: a submission is not a closure.
        """
        from backend.options.protection.exit_builder import build_structure_exit_orders

        specs, legs = self._parent_leg_outcomes(parent)
        proven: Dict[str, int] = {}
        for step_no in tuple(getattr(spec, "depends_on", ()) or ()):
            closing = specs.get(int(step_no))
            if closing is None:
                continue
            entry = dict(legs.get(int(step_no)) or {})
            filled = int(entry.get("filled_quantity") or 0)
            if filled:
                symbol = str(getattr(closing, "tradingsymbol", "") or "")
                proven[symbol] = proven.get(symbol, 0) + filled
        rows: List[Dict[str, Any]] = []
        for item in ((parent or {}).get("step_spec") or []):
            current = int(getattr(item, "current_quantity", 0) or 0)
            if current == 0:
                continue
            option = dict((getattr(item, "detail", {}) or {}).get("option") or {})
            rows.append(
                {
                    "tradingsymbol": str(getattr(item, "tradingsymbol", "") or ""),
                    "side": "SELL" if current < 0 else "BUY",
                    # SIGNED, exactly as the exit builder expects a position: a
                    # short is negative (its close is a BUY), a long is positive.
                    "quantity": int(current),
                    "exchange": str(getattr(item, "exchange", "") or "NFO"),
                    "product": str(getattr(item, "product", "") or ""),
                    "underlying": str(option.get("underlying") or ""),
                    "expiry": str(option.get("expiry") or ""),
                }
            )
        ordered, meta = build_structure_exit_orders(rows, closed_short_quantities=proven)
        symbol = str(getattr(spec, "tradingsymbol", "") or "")
        released = any(str(order.get("tradingsymbol") or "") == symbol for order in ordered)
        detail = {
            "rule": RULE_HEDGE_RELEASE_WITHHELD,
            "step_no": int(getattr(spec, "step_no", 0) or 0),
            "depends_on": [int(value) for value in (getattr(spec, "depends_on", ()) or ())],
            "tradingsymbol": symbol,
            "proven_short_closures": dict(proven),
            "short_plan": list(meta.get("short_plan") or []),
            "withheld_hedges": list(meta.get("withheld_hedges") or []),
            "released_tradingsymbols": [
                str(order.get("tradingsymbol") or "") for order in ordered
            ],
        }
        if not released:
            return False, BLOCKER_HEDGE_SHORT_NOT_CLOSED, detail
        return True, "", detail

    def _mis_context(
        self, *, plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """The platform's own MIS release inputs: heartbeat, policy, stop."""
        from sqlalchemy import text

        run_id = str(binding.get("strategy_run_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        with self.session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT s.stale_exit_policy,
                           r.last_heartbeat_at,
                           r.runtime_state_json,
                           j.desired_state
                    FROM public.hosted_strategies s
                    LEFT JOIN public.algo_worker_runs r ON r.strategy_run_id = :run_id
                    LEFT JOIN public.strategy_jobs j ON j.run_id = :run_id
                    WHERE s.id = :strategy_id
                    ORDER BY j.created_at DESC
                    LIMIT 1
                    """
                ),
                {"run_id": run_id, "strategy_id": strategy_id},
            ).mappings().first()
        if row is None:
            return {}
        runtime = row["runtime_state_json"]
        if isinstance(runtime, str):
            import json as _json

            try:
                runtime = _json.loads(runtime or "{}")
            except ValueError:
                runtime = {}
        operations = ((runtime or {}).get("backend_protection") or {}).get("operations") or {}
        armed = bool(operations.get("exit_on_worker_stale"))
        stale_seconds = operations.get("worker_stale_sec")
        return {
            "stale_exit_armed": armed,
            "last_heartbeat_at": row["last_heartbeat_at"],
            "worker_stale_sec": stale_seconds,
            "stop_requested": str(row["desired_state"] or "") == "stopped",
            "policy": str(row["stale_exit_policy"] or ""),
        }

    def _record_release_trail(
        self, *, plan_id: str, spec: Any, actor: str, released_state: str
    ) -> None:
        """Append the trail event a released dependent step never got at freeze."""
        stored = self.sequence.submissions.get(plan_id=plan_id, step_no=int(spec.step_no)) or {}
        snapshot = dict(stored.get("delta_snapshot") or {})
        detail = {**snapshot, **dict(stored.get("detail") or {})}
        detail = {"state": released_state, **detail}
        broker_order_ids = [str(value) for value in (stored.get("broker_order_ids") or [])]
        event = {
            "no_op": "no_op",
            "rejected": "rejected",
            "filled": "filled",
        }.get(released_state, "submitted")
        self._trail._record_event(
            plan_id,
            step_no=int(spec.step_no),
            event=event,
            refusal_reason=("LIVE_ORDER_REJECTED" if released_state == "rejected" else None),
            actor_id=actor,
            detail=detail,
            broker_order_id=(broker_order_ids[0] if broker_order_ids else None),
            at=self._clock(),
        )

    def _record_mis_squareoff_evidence(
        self,
        *,
        plan: Mapping[str, Any],
        spec: Any,
        parent: Mapping[str, Any],
        authority: Mapping[str, Any],
        released_state: str,
        rule_detail: Mapping[str, Any],
    ) -> None:
        """Record the platform's OWN square-off evidence for a released exit.

        A failed square-off is ``action_required`` and keeps reconciling; it is
        explicitly not settlement. Nothing here guesses an exchange close: the
        evidence names the clock (or the stop / stale-worker policy) that released
        the exit and the quantity that went through the durable claim path.
        """
        from backend.strategies.mis_squareoff import MisSquareoffEvidenceStore, SquareoffRecord

        plan_id = str(plan.get("plan_id") or "")
        stored = self.sequence.submissions.get(plan_id=plan_id, step_no=int(spec.step_no)) or {}
        delta = dict(stored.get("delta_snapshot") or {})
        stored_detail = dict(stored.get("detail") or {})
        mis = dict((getattr(spec, "detail", {}) or {}).get("mis") or {})
        exchange = str(mis.get("exchange") or getattr(spec, "exchange", "") or "")
        now = self._session_clock()
        outcome = "action_required" if released_state == "rejected" else "squared_off"
        scheduled = rule_detail.get("due_at") or rule_detail.get("scheduled_at")
        scheduled_at = now
        if scheduled:
            parsed = _as_datetime(scheduled)
            if parsed is not None:
                scheduled_at = parsed
        try:
            MisSquareoffEvidenceStore(session_factory=self.session_factory).record(
                SquareoffRecord(
                    account_id=str(plan.get("account_id") or ""),
                    strategy_id=str(plan.get("strategy_id") or ""),
                    strategy_run_id=str(authority.get("worker_run_id") or ""),
                    product="MIS",
                    session_date=now.date(),
                    exchange=exchange or "NSE",
                    scheduled_at=scheduled_at,
                    outcome=outcome,
                    exit_claim_id=str(spec.step_ref),
                    detail={
                        "plan_id": plan_id,
                        "step_no": int(spec.step_no),
                        "lane": str(parent.get("lane") or ""),
                        "release_authority": str(rule_detail.get("authority_source") or ""),
                        "frozen_quantity": int(
                            (getattr(spec, "detail", {}) or {}).get("sizing", {}).get("quantity")
                            or spec.quantity
                            or 0
                        ),
                        "released_state": str(released_state),
                        # What actually went through the durable claim path, not
                        # what the frozen step asked for.
                        "released_quantity": int(
                            stored_detail.get("released_quantity")
                            or (
                                int(delta.get("quantity") or 0)
                                if str(released_state) != "no_op"
                                else 0
                            )
                        ),
                        "released_side": str(
                            stored_detail.get("side") or delta.get("side") or ""
                        ),
                        "attributed_quantity": delta.get("current"),
                        "attempt": int(authority.get("attempt") or 0),
                        "target_quantity": int(spec.target_quantity),
                    },
                )
            )
        except Exception:  # noqa: BLE001 - the trail still names the release
            pass


def live_execution_enabled(session_factory: Optional[Callable[[], Any]] = None) -> bool:  # pragma: no cover - convenience
    return hosted_live_enabled()


#: The plan kind each public lane is entered through. A lane is advertised only
#: while the EXECUTOR also admits its plan kind, so the capability surface and the
#: submission gate can never disagree.
_LANE_PLAN_KINDS = {
    "cnc": ("target_weights", "single_instrument"),
    "mis": ("single_instrument",),
    "futures": ("target_futures",),
    "options": ("option_structure",),
}


def hosted_live_lanes() -> list:
    """The live lanes this deployment can actually execute.

    Two independent gates must agree: the lane's step builder must be registered
    (``live_sequence.hosted_live_lanes``) AND the executor must admit the plan kind
    the lane is entered through. Anything else is not advertised.
    """
    from .live_sequence import hosted_live_lanes as _registered_lanes

    return [
        lane
        for lane in _registered_lanes()
        if all(kind in LIVE_PLAN_KINDS for kind in _LANE_PLAN_KINDS.get(lane, ()))
    ]
