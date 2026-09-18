"""The paper plan executor: the first consumer of a frozen plan (Project 6, D-2).

The chain — proposal → validated plan → paper admission + reservation →
**execution** → attributed paper fills → settlement — is exercised end to end
for the first time here, on paper accounts only (D-1). The executor:

* fails closed through a precondition chain where every refusal is NAMED and
  lands in the append-only event trail (``PAPER_ONLY_EXECUTION`` for anything
  that is not a paper book — live is a separate authorization that does not
  exist yet);
* derives each step from the plan's resolved representation (target − current
  attributed book, floored to the pinned catalog's lot size; a zero delta is a
  real ``no_op`` outcome, not an absence);
* submits through the EXISTING paper runtime
  (``PaperTradingService.place_order``) with attribution carrying the BOUND
  run's ``strategy_run_id`` plus the plan/reservation/step refs, so G1's paper
  fold attributes the fills and the whole audit chain is recoverable from the
  order metadata alone;
* consumes the reservation on fills and releases it ``terminal_unfilled`` on
  rejections (D-4) — Phase 4's deliberately-open lifecycle closes here;
* records work events on the settlement barrier (created on submission,
  resolved on a terminal outcome) and leaves settlement assessment to its own
  surface — an observer, never a new settlement semantics (D-5).

Execution events are append-only facts (D-3): current step state is derived
from the trail, never stored, so history cannot be rewritten into something
that did not happen. Unknown execution state (a runtime error, an accepted
order that never reached a terminal outcome) records ``failed`` and HOLDS the
reservation — capacity is never released on a guess.

Concurrency: one plan executes at most once. The ``submitted`` event and the
already-executing check commit inside ONE transaction that holds the plan's
advisory lock on PostgreSQL, so two concurrent executes produce exactly one
submission; the loser refuses ``PLAN_ALREADY_EXECUTED`` from the committed
trail. In-process threads are additionally serialized on a per-plan lock.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import select, text

from backend.strategies.attribution_models import (
    EXECUTABLE_PLAN_KINDS,
    StrategyPositionProjection,
    StrategyProposal,
    StrategyRunBinding,
)
from backend.strategies.reservations import (
    HOLDING_STATUSES,
    ReservationLedger,
    _as_datetime,
)
from backend.strategies.settlement import ExecutionBarrier

#: The paper book this executor executes. Anything else (live, dry_run) is a
#: different authorization and a different book: refused by name.
PAPER_ENVIRONMENT = "paper"

#: Refusal vocabulary. Every refusal is an event with its reason (D-3) and a
#: catchable exception with the same name — an operator never has to guess.
REFUSAL_REASONS = (
    "PLAN_KIND_UNSUPPORTED",
    "PLAN_NOT_VALIDATED",
    "RESERVATION_REQUIRED",
    "PAPER_ONLY_EXECUTION",
    "RESERVATION_EXPIRED",
    "STRATEGY_RUN_BINDING_MISSING",
    "ACCOUNT_SCOPE_MISMATCH",
    "PLAN_ALREADY_EXECUTED",
    "PAPER_ORDER_REJECTED",
    "PAPER_EXECUTION_FAILED",
)

#: Reservation statuses from which this plan may still begin executing.
EXECUTABLE_RESERVATION_STATUSES = ("active", "renewed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionRefusal(Exception):
    """A named, fail-closed refusal from the executor (D-2)."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


class PaperPlanExecutor:
    """Executes one validated, admitted paper plan through the paper runtime."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        paper_service: Any = None,
        *,
        barrier: Optional[ExecutionBarrier] = None,
        ledger: Optional[ReservationLedger] = None,
        clock: Optional[Callable[[], datetime]] = None,
        paper_service_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        if paper_service is None and paper_service_factory is not None:
            paper_service = paper_service_factory()
        self._paper_service = paper_service
        self.paper_service_factory = paper_service_factory
        self.barrier = barrier or ExecutionBarrier(session_factory=session_factory)
        self.ledger = ledger or ReservationLedger(session_factory=session_factory)
        self._clock = clock or _utcnow
        self._plan_locks: Dict[str, threading.Lock] = {}
        self._plan_locks_guard = threading.Lock()

    # ------------------------------------------------------------------ entry

    async def execute(self, plan: Mapping[str, Any], *, actor: str) -> Dict[str, Any]:
        """Execute one plan on paper, or refuse by name with a trail event."""
        plan_id = str(plan.get("plan_id") or "")
        plan_kind = str(plan.get("plan_kind") or "")
        reservation = self.ledger.for_plan(plan_id)
        try:
            envelope = self._envelope(plan)
            self._preconditions(plan, envelope, reservation)
            binding = self._binding(envelope, plan)
        except ExecutionRefusal as exc:
            self._record_event(
                plan_id,
                step_no=1,
                event="rejected",
                refusal_reason=exc.reason_code,
                actor_id=actor,
                detail=exc.detail,
            )
            raise

        steps_spec = self._plan_steps(plan, binding)
        base = self._clock()

        outcomes: List[Dict[str, Any]] = []
        order_ids: List[str] = []
        filled_ids: List[str] = []
        rejected = False
        failed = False
        filled = False
        submitted_any = False
        counter = 0

        def _stamp() -> datetime:
            nonlocal counter
            counter += 1
            return base + timedelta(microseconds=counter)

        for step_no, leg, quantity, side in steps_spec:
            if quantity == 0:
                outcomes.append(
                    self._record_event(
                        plan_id,
                        step_no=step_no,
                        event="no_op",
                        actor_id=actor,
                        detail={
                            "instrument_id": leg.get("instrument_id"),
                            "target_quantity": leg.get("signed_quantity"),
                            "current_quantity": leg.get("_current_quantity"),
                            "message": "The attributed book already sits at the plan's target",
                        },
                        at=_stamp(),
                    )
                )
                continue

            submission = await self._submit_step(
                plan, reservation, actor, step_no=step_no, leg=leg, quantity=quantity,
                side=side, binding=binding, at=_stamp(),
            )
            outcomes.append(submission["outcome"])
            submitted_any = True
            if submission["order_id"]:
                order_ids.append(submission["order_id"])
            if submission["outcome"]["event"] == "filled":
                filled_ids.append(submission["order_id"] or "")
            rejected = rejected or submission["outcome"]["event"] == "rejected"
            failed = failed or submission["outcome"]["event"] == "failed"
            filled = filled or submission["outcome"]["event"] == "filled"

        self._settle_reservation(
            reservation, actor, filled_ids=filled_ids, failed=failed, submitted_any=submitted_any
        )

        if failed:
            status = "failed"
        elif filled:
            status = "filled"
        elif rejected:
            status = "rejected"
        else:
            status = "no_op"
        return {
            "plan_id": plan_id,
            "status": status,
            "steps": outcomes,
            "reservation_id": reservation["reservation_id"] if reservation else None,
            "paper_order_ids": order_ids,
        }

    # ---------------------------------------------------------- preconditions

    def _envelope(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        from backend.strategies.proposals import ProposalStore

        envelope = ProposalStore(session_factory=self.session_factory).get_proposal(
            str(plan.get("proposal_id") or "")
        )
        if envelope is None:
            raise ExecutionRefusal(
                "PLAN_NOT_VALIDATED",
                {"plan_id": str(plan.get("plan_id") or ""), "message": "No proposal envelope exists for this plan"},
            )
        return envelope

    def _preconditions(
        self, plan: Mapping[str, Any], envelope: Mapping[str, Any], reservation: Optional[Dict[str, Any]]
    ) -> None:
        """The fail-closed chain, in order. Every exit is named (D-2)."""
        plan_id = str(plan.get("plan_id") or "")

        # 1. Only plan kinds the executor can act on. ``target_weights`` stays a
        #    valid plan kind — it simply has no executor in this phase.
        if plan.get("plan_kind") not in EXECUTABLE_PLAN_KINDS:
            raise ExecutionRefusal(
                "PLAN_KIND_UNSUPPORTED",
                {"plan_id": plan_id, "plan_kind": str(plan.get("plan_kind") or "")},
            )
        # 2. A frozen plan executes only while its envelope is validated.
        if str(envelope.get("status") or "") != "validated":
            raise ExecutionRefusal(
                "PLAN_NOT_VALIDATED",
                {"plan_id": plan_id, "proposal_status": str(envelope.get("status") or "")},
            )
        # 3. An active reservation must exist: execution CONSUMES admission (D-1).
        if reservation is None or str(reservation.get("status")) not in EXECUTABLE_RESERVATION_STATUSES:
            raise ExecutionRefusal(
                "RESERVATION_REQUIRED",
                {
                    "plan_id": plan_id,
                    "reservation_status": None if reservation is None else str(reservation.get("status")),
                },
            )
        # 4. Paper accounts only, ever. Live enablement is a separate,
        #    not-yet-existing authorization (phase boundary).
        environment = str(reservation.get("execution_environment") or "")
        if environment != PAPER_ENVIRONMENT:
            raise ExecutionRefusal(
                "PAPER_ONLY_EXECUTION",
                {
                    "plan_id": plan_id,
                    "execution_environment": environment,
                    "message": "This phase executes paper accounts only; live is a separate authorization",
                },
            )
        # 5. Validity: an expired reservation is no authority to create exposure.
        valid_until = _as_datetime(reservation.get("valid_until"))
        if valid_until is not None and self._clock() >= valid_until:
            raise ExecutionRefusal(
                "RESERVATION_EXPIRED",
                {
                    "plan_id": plan_id,
                    "reservation_id": str(reservation.get("reservation_id")),
                    "valid_until": valid_until.isoformat(),
                },
            )

    def _binding(self, envelope: Mapping[str, Any], plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The attribution target must exist and agree with the plan's scope."""
        run_id = str(envelope.get("strategy_run_id") or "")
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyRunBinding).where(StrategyRunBinding.strategy_run_id == run_id)
            ).scalar_one_or_none()
        if row is None:
            raise ExecutionRefusal(
                "STRATEGY_RUN_BINDING_MISSING",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "strategy_run_id": run_id,
                    "message": "No bound run exists to attribute the fills to",
                },
            )
        binding = {
            "strategy_run_id": str(row.strategy_run_id),
            "strategy_id": str(row.strategy_id),
            "owner_id": str(row.owner_id),
            "account_id": str(row.account_id),
            "execution_environment": str(row.execution_environment),
        }
        if (
            binding["account_id"] != str(plan.get("account_id") or "")
            or binding["strategy_id"] != str(plan.get("strategy_id") or "")
            or binding["execution_environment"] != PAPER_ENVIRONMENT
        ):
            raise ExecutionRefusal(
                "ACCOUNT_SCOPE_MISMATCH",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "binding_account_id": binding["account_id"],
                    "plan_account_id": str(plan.get("account_id") or ""),
                    "binding_environment": binding["execution_environment"],
                },
            )
        return binding

    # ----------------------------------------------------------------- steps

    def _plan_steps(
        self, plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> List[Any]:
        """Derive the steps from the resolved representation (D-2).

        One step per leg: side + quantity = target − current attributed book,
        floored to the pinned catalog's lot size when it provides lots. The
        private ``_current_quantity`` entry travels with the leg for evidence.
        """
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        steps: List[Any] = []
        for index, leg in enumerate(legs, start=1):
            leg = dict(leg)
            current = self._current_book_quantity(plan, leg, binding)
            leg["_current_quantity"] = current
            target = int(leg.get("signed_quantity") or 0)
            delta = target - current
            lot = self._lot_size(str(leg.get("instrument_id") or ""))
            quantity = self._floor_to_lot(delta, lot)
            side = "BUY" if quantity > 0 else "SELL"
            steps.append((index, leg, quantity, side))
        return steps

    def _current_book_quantity(
        self, plan: Mapping[str, Any], leg: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> int:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyPositionProjection.net_quantity).where(
                    StrategyPositionProjection.account_id == str(plan.get("account_id") or ""),
                    StrategyPositionProjection.strategy_id == str(plan.get("strategy_id") or ""),
                    StrategyPositionProjection.execution_environment == PAPER_ENVIRONMENT,
                    StrategyPositionProjection.identity_kind == "canonical",
                    StrategyPositionProjection.canonical_instrument_id
                    == str(leg.get("instrument_id") or ""),
                    StrategyPositionProjection.product == str(leg.get("product") or ""),
                )
            ).scalars().all()
        return int(sum(int(value or 0) for value in rows))

    def _lot_size(self, instrument_id: str) -> int:
        """The pinned catalog's lot for the instrument; ``1`` when it provides none.

        ``instrument_catalog_records`` is a pre-existing platform table with no
        ORM model, so the read is ``public.``-qualified SQL (the established
        pattern for platform tables).
        """
        if not instrument_id:
            return 1
        with self.session_factory() as session:
            row = session.execute(
                text(
                    "SELECT lot_size FROM public.instrument_catalog_records "
                    "WHERE instrument_id = :iid"
                ),
                {"iid": instrument_id},
            ).fetchone()
        try:
            lot = int(row[0]) if row is not None and row[0] is not None else 1
        except (TypeError, ValueError):
            return 1
        return lot if lot > 0 else 1

    @staticmethod
    def _floor_to_lot(delta: int, lot: int) -> int:
        if delta == 0 or lot <= 1:
            return delta
        sign = 1 if delta > 0 else -1
        floored = (abs(delta) // lot) * lot
        return sign * floored

    # ------------------------------------------------------------ submission

    async def _submit_step(
        self,
        plan: Mapping[str, Any],
        reservation: Dict[str, Any],
        actor: str,
        *,
        step_no: int,
        leg: Mapping[str, Any],
        quantity: int,
        side: str,
        binding: Mapping[str, Any],
        at: datetime,
    ) -> Dict[str, Any]:
        """Submit one step through the paper runtime; record both trail events.

        The ``submitted`` event and the already-executing check commit in ONE
        transaction under the plan's lock (plus the PostgreSQL advisory lock),
        so concurrent executes of the same plan yield exactly ONE submission.
        """
        plan_id = str(plan.get("plan_id") or "")
        ref = f"plan:{plan_id}:step:{step_no}"
        with self._plan_lock(plan_id):
            with self.session_factory() as session:
                if session.bind.dialect.name == "postgresql":
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": f"plan-exec:{plan_id}"},
                    )
                prior = session.execute(
                    text(
                        "SELECT COUNT(*) FROM strategy_plan_execution_events "
                        "WHERE plan_id = :plan_id"
                    ),
                    {"plan_id": plan_id},
                ).scalar()
                if int(prior or 0) > 0:
                    raise ExecutionRefusal(
                        "PLAN_ALREADY_EXECUTED",
                        {
                            "plan_id": plan_id,
                            "message": "This plan already has an execution trail; a plan executes once",
                        },
                    )
                self._write_event(
                    session,
                    plan_id=plan_id,
                    step_no=step_no,
                    event="submitted",
                    actor_id=actor,
                    detail={
                        "instrument_id": leg.get("instrument_id"),
                        "tradingsymbol": leg.get("tradingsymbol"),
                        "side": side,
                        "quantity": quantity,
                        "reservation_id": reservation["reservation_id"],
                        "strategy_run_id": binding["strategy_run_id"],
                    },
                    at=at,
                )
                session.commit()

        # Work exists from the moment capital is committed to the runtime (D-5).
        self.barrier.record_work_event(
            account_id=str(plan.get("account_id") or ""),
            strategy_id=str(plan.get("strategy_id") or ""),
            execution_environment=PAPER_ENVIRONMENT,
            event="work_created",
            ref=ref,
            detail={"plan_id": plan_id, "step_no": step_no, "quantity": quantity, "side": side},
        )

        attribution = {
            "strategy_run_id": binding["strategy_run_id"],
            "strategy_id": str(plan.get("strategy_id") or ""),
            "plan_id": plan_id,
            "reservation_id": reservation["reservation_id"],
            "step_no": step_no,
            "execution_mode": PAPER_ENVIRONMENT,
            "source": "hosted_plan_execution",
        }
        order_payload = {
            "exchange": str(leg.get("broker_exchange") or leg.get("exchange") or ""),
            "tradingsymbol": str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
            "transaction_type": side,
            "product": str(leg.get("product") or ""),
            "order_type": "MARKET",
            "quantity": int(quantity),
        }

        outcome: Dict[str, Any]
        order_id: Optional[str] = None
        try:
            service = self._paper_service
            if service is None and self.paper_service_factory is not None:
                service = self.paper_service_factory()
            if service is None:
                raise RuntimeError("no paper runtime is wired into this executor")
            result = await service.place_order(
                account_scope=str(reservation.get("account_id")),
                order_payload=order_payload,
                attribution=attribution,
            )
        except Exception as exc:  # noqa: BLE001 - unknown state, never a guess
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="failed",
                actor_id=actor,
                detail={"error": str(exc), "ref": ref},
                at=at,
            )
            return {"outcome": outcome, "order_id": None}

        order = dict(result.get("order") or {})
        order_id = str(order.get("order_id") or "") or None
        status = str(result.get("status") or "")

        if status == "filled":
            filled = int(order.get("filled_quantity") or order.get("quantity") or 0)
            self.barrier.record_work_event(
                account_id=str(plan.get("account_id") or ""),
                strategy_id=str(plan.get("strategy_id") or ""),
                execution_environment=PAPER_ENVIRONMENT,
                event="work_resolved",
                ref=ref,
                detail={"plan_id": plan_id, "step_no": step_no, "outcome": "filled"},
            )
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="filled",
                paper_order_id=order_id,
                filled_quantity=filled,
                actor_id=actor,
                detail={
                    "fill_price": str(order.get("average_price") or ""),
                    "tradingsymbol": order.get("tradingsymbol"),
                },
                at=at,
            )
        elif status == "rejected":
            reason = str(result.get("reason") or "rejected by the paper runtime")
            self.barrier.record_work_event(
                account_id=str(plan.get("account_id") or ""),
                strategy_id=str(plan.get("strategy_id") or ""),
                execution_environment=PAPER_ENVIRONMENT,
                event="work_resolved",
                ref=ref,
                detail={"plan_id": plan_id, "step_no": step_no, "outcome": "rejected"},
            )
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="rejected",
                paper_order_id=order_id,
                refusal_reason="PAPER_ORDER_REJECTED",
                actor_id=actor,
                detail={"reason": reason, "ref": ref},
                at=at,
            )
        else:
            # Accepted-but-unterminal (or anything unknown): execution state is
            # genuinely unknown — record ``failed`` honestly and hold capacity.
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="failed",
                paper_order_id=order_id,
                actor_id=actor,
                detail={"runtime_status": status or "unknown", "ref": ref},
                at=at,
            )
        return {"outcome": outcome, "order_id": order_id}

    # ----------------------------------------------------------- reservation

    def _settle_reservation(
        self,
        reservation: Dict[str, Any],
        actor: str,
        *,
        filled_ids: List[str],
        failed: bool,
        submitted_any: bool,
    ) -> None:
        """Consume on fills, release terminal-unfilled on rejections, hold on doubt (D-4).

        A plan whose every step was ``no_op`` committed no capital, so its
        reservation stays exactly as admission left it — nothing happened.
        """
        reservation_id = str(reservation.get("reservation_id") or "")
        if not reservation_id:
            return
        if filled_ids:
            self.ledger.consume(
                reservation_id,
                actor_id=actor,
                detail={
                    "plan_id": reservation.get("plan_id"),
                    "paper_order_ids": list(filled_ids),
                },
            )
        elif submitted_any and not failed:
            self.ledger.release(reservation_id, reason="terminal_unfilled", actor_id=actor)
        # Anything else (all no_op, or any unknown state) holds the capacity:
        # release requires proof, never a guess.

    # ----------------------------------------------------------- event trail

    def _record_event(
        self,
        plan_id: str,
        *,
        step_no: int,
        event: str,
        actor_id: str,
        detail: Optional[Mapping[str, Any]] = None,
        paper_order_id: Optional[str] = None,
        filled_quantity: Optional[int] = None,
        refusal_reason: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self.session_factory() as session:
            row = self._write_event(
                session,
                plan_id=plan_id,
                step_no=step_no,
                event=event,
                actor_id=actor_id,
                detail=detail,
                paper_order_id=paper_order_id,
                filled_quantity=filled_quantity,
                refusal_reason=refusal_reason,
                at=at or self._clock(),
            )
            session.commit()
            return {
                "step_no": int(row.step_no),
                "event": str(row.event),
                "paper_order_id": row.paper_order_id,
                "filled_quantity": row.filled_quantity,
                "refusal_reason": row.refusal_reason,
                "detail": dict(row.detail or {}),
            }

    @staticmethod
    def _write_event(
        session: Any,
        *,
        plan_id: str,
        step_no: int,
        event: str,
        actor_id: str,
        detail: Optional[Mapping[str, Any]] = None,
        paper_order_id: Optional[str] = None,
        filled_quantity: Optional[int] = None,
        refusal_reason: Optional[str] = None,
        at: Optional[datetime] = None,
    ):
        from backend.strategies.attribution_models import StrategyPlanExecutionEvent

        row = StrategyPlanExecutionEvent(
            id=str(uuid.uuid4()),
            plan_id=plan_id,
            step_no=int(step_no),
            event=event,
            paper_order_id=paper_order_id,
            filled_quantity=filled_quantity,
            refusal_reason=refusal_reason,
            actor_id=actor_id,
            detail=dict(detail or {}),
            created_at=at or _utcnow(),
        )
        session.add(row)
        session.flush()
        return row

    # ------------------------------------------------------------- locking

    def _plan_lock(self, plan_id: str):
        """The in-process serialization point (PostgreSQL adds the advisory lock)."""
        with self._plan_locks_guard:
            lock = self._plan_locks.get(plan_id)
            if lock is None:
                lock = threading.Lock()
                self._plan_locks[plan_id] = lock
            return lock
