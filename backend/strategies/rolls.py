"""The roll state machine: acquire, prove, then — only then — release (D-2).

R3 §13 locked decision 6 is a **new** invariant, not a re-use of anything that
already existed. A roll acquires the replacement contract first, and the old
contract's close step is released only once the FULL required replacement quantity
is *proven* filled.

Two words in that sentence carry the whole design:

* **proven** means the strategy's attributed book on the new contract, read from the
  G1 projection — not an order-status label. A broker that says "complete" is
  reporting what it did with an order; the book reports what the strategy actually
  holds, which is the thing a roll is trying to keep continuous.
* **full** means full. A partial or stalled replacement marks the roll
  ``action_required``, keeps the old contract's attribution intact, and never
  auto-reverses — because reversing would close a position the strategy still holds
  on the strength of a fill that did not happen.

This is deliberately NOT the basket ``all_or_none`` flag. That flag is a
basket-atomicity label with no roll semantics, however similar the names sound, and
the negative test pins that toggling it changes nothing here. *Proportional release*
is a future opt-in that requires its own decision and appears nowhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    StrategyPositionProjection,
    StrategyRoll,
    StrategyRollEvent,
)

#: The ordered lifecycle. The order IS the invariant.
ROLL_STATES = ("acquiring", "proving_filled", "releasing_old", "completed", "action_required")

#: States from which a roll is still in flight. ``action_required`` belongs here
#: deliberately: a stalled roll is unresolved, and a second roll on the same old
#: contract would fight it for the same transition.
OPEN_ROLL_STATES = ("acquiring", "proving_filled", "releasing_old", "action_required")

#: The event vocabulary (mirrors ``ck_roll_event``).
ROLL_EVENTS = (
    "created",
    "acquired",
    "replacement_filled",
    "fill_proven",
    "close_released",
    "old_flat",
    "completed",
    "stalled",
    "escalated",
)

#: The role a frozen plan plays in a roll. It travels WITH the plan, so the
#: executor can enforce the contract without being told by its caller.
ROLL_PLAN_ROLES = ("open_new", "close_old")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RollError(Exception):
    reason_code = "ROLL_ERROR"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


class RollStateError(RollError):
    reason_code = "ROLL_STATE_INVALID"


class RollDuplicate(RollError):
    """One open roll per (strategy, old contract). A second would fight the first."""

    reason_code = "ROLL_ALREADY_OPEN"


class ReleaseRefused(RollError):
    """The close step is unreachable before the full replacement is proven filled."""

    reason_code = "ROLL_FILL_NOT_PROVEN"


class RollPlanMismatch(RollError):
    """The plan a roll names is not this strategy's/account's frozen plan."""

    reason_code = "ROLL_PLAN_MISMATCH"


class RollCloseNotReleased(RollError):
    """A close was attempted before the roll released it."""

    reason_code = "ROLL_CLOSE_NOT_RELEASED"


class RollUnknown(RollError):
    """A plan names a roll that does not exist for this strategy."""

    reason_code = "ROLL_UNKNOWN"


class RollNotFlat(RollError):
    """The old book is not proven flat, so the roll cannot complete."""

    reason_code = "ROLL_OLD_NOT_FLAT"


class RollStateMachine:
    """The durable ordered roll. It decides; it never places an order itself."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        notifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        positions_reader: Optional[Callable[..., int]] = None,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._notifier = notifier
        self._positions_reader = positions_reader

    # -- reads --------------------------------------------------------------

    def attributed_quantity(
        self, *, strategy_id: str, account_id: str, instrument_id: str,
        product: str = "NRML", execution_environment: str = "paper",
    ) -> int:
        """The strategy's attributed book on one contract, from the G1 projection.

        This is the proof surface, and it is deliberately the book rather than an
        order status: the book says what the strategy holds, which is what a roll
        needs to keep continuous.
        """
        if self._positions_reader is not None:
            return int(
                self._positions_reader(
                    strategy_id=strategy_id, account_id=account_id,
                    instrument_id=instrument_id, product=product,
                    execution_environment=execution_environment,
                )
                or 0
            )
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyPositionProjection.net_quantity).where(
                    StrategyPositionProjection.account_id == str(account_id),
                    StrategyPositionProjection.strategy_id == str(strategy_id),
                    StrategyPositionProjection.execution_environment
                    == str(execution_environment),
                    StrategyPositionProjection.identity_kind == "canonical",
                    StrategyPositionProjection.canonical_instrument_id == str(instrument_id),
                    StrategyPositionProjection.product == str(product),
                )
            ).scalars().all()
        return int(sum(int(value or 0) for value in rows))

    def get(self, roll_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyRoll).where(StrategyRoll.roll_id == str(roll_id))
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def open_for(self, *, strategy_id: str, old_instrument_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyRoll).where(
                    StrategyRoll.strategy_id == str(strategy_id),
                    StrategyRoll.old_instrument_id == str(old_instrument_id),
                    StrategyRoll.state.in_(OPEN_ROLL_STATES),
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def list_for_strategy(self, *, strategy_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyRoll)
                .where(StrategyRoll.strategy_id == str(strategy_id))
                .order_by(StrategyRoll.created_at.desc())
                .limit(int(limit))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def for_plan(self, *, strategy_id: str, plan_id: str) -> Optional[Dict[str, Any]]:
        """The roll a frozen plan belongs to, if any.

        A plan may name an explicit ``roll_id``; when it does not, the roll that
        names the plan is the binding. Both are durable lookups, never caller
        claims, and a roll of another strategy is not a match.
        """
        if not plan_id:
            return None
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyRoll)
                .where(
                    StrategyRoll.strategy_id == str(strategy_id),
                    StrategyRoll.plan_id == str(plan_id),
                )
                .order_by(StrategyRoll.created_at.desc())
            ).scalars().first()
            return self._view(row) if row is not None else None

    def by_id(self, *, strategy_id: str, roll_id: str) -> Optional[Dict[str, Any]]:
        """One roll, only when it belongs to this strategy."""
        roll = self.get(roll_id)
        if roll is None or str(roll["strategy_id"]) != str(strategy_id):
            return None
        return roll

    def _validate_plan_binding(
        self,
        *,
        strategy_id: str,
        account_id: str,
        plan_id: str,
        old_instrument_id: str,
        new_instrument_id: str,
    ) -> int:
        """The named plan is the APPROVED ACQUISITION plan; return its quantity.

        A roll is opened by the plan that acquires the replacement, and that plan
        carries the replacement contract only - the old-contract close is its own
        plan (the frozen close plan the executor releases later). Requiring both
        contracts in one plan contradicted the executor's rule that a plan may
        only address its own half.

        The quantity is AUTHORITATIVE from the plan, not from the caller: a caller
        may not lower the required replacement below what the approved plan buys.
        """
        from backend.strategies.attribution_models import StrategyPlan

        with self.session_factory() as session:
            row = session.execute(
                select(StrategyPlan).where(StrategyPlan.plan_id == str(plan_id))
            ).scalar_one_or_none()
        if row is None:
            raise RollPlanMismatch(
                {"plan_id": str(plan_id), "message": "no frozen plan with this id"}
            )
        if str(row.strategy_id) != str(strategy_id) or str(row.account_id) != str(account_id):
            raise RollPlanMismatch(
                {
                    "plan_id": str(plan_id),
                    "plan_strategy_id": str(row.strategy_id),
                    "plan_account_id": str(row.account_id),
                    "strategy_id": str(strategy_id),
                    "account_id": str(account_id),
                    "message": "the plan belongs to another strategy or account",
                }
            )
        legs = list((row.resolved_plan or {}).get("legs") or [])
        instruments = {str(leg.get("instrument_id") or "") for leg in legs}
        if str(old_instrument_id) in instruments:
            # A plan that carries the old contract is not an acquisition plan.
            raise RollPlanMismatch(
                {
                    "plan_id": str(plan_id),
                    "old_instrument_id": str(old_instrument_id),
                    "message": (
                        "this plan carries the old contract: the roll is opened by the "
                        "approved ACQUISITION plan, and the old-contract close is its own plan"
                    ),
                }
            )
        acquisition = [
            leg for leg in legs if str(leg.get("instrument_id") or "") == str(new_instrument_id)
        ]
        if not acquisition:
            raise RollPlanMismatch(
                {
                    "plan_id": str(plan_id),
                    "new_instrument_id": str(new_instrument_id),
                    "plan_instruments": sorted(instruments),
                    "message": "the frozen plan does not carry the replacement contract",
                }
            )
        leg = acquisition[0]
        quantity = leg.get("quantity")
        if quantity is None:
            quantity = abs(int(leg.get("signed_quantity") or 0))
        quantity = int(quantity or 0)
        if quantity <= 0:
            raise RollPlanMismatch(
                {
                    "plan_id": str(plan_id),
                    "new_instrument_id": str(new_instrument_id),
                    "quantity": quantity,
                    "message": "the approved plan names no replacement quantity",
                }
            )
        return quantity

    def events(self, roll_id: str) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyRollEvent)
                .where(StrategyRollEvent.roll_id == str(roll_id))
                .order_by(StrategyRollEvent.created_at, StrategyRollEvent.event)
            ).scalars().all()
            return [{"event": str(row.event), "detail": dict(row.detail or {})} for row in rows]

    # -- lifecycle ----------------------------------------------------------

    def create(
        self,
        *,
        strategy_id: str,
        account_id: str,
        old_instrument_id: str,
        new_instrument_id: str,
        required_replacement_quantity: int,
        old_coordinate: Optional[Mapping[str, Any]] = None,
        new_coordinate: Optional[Mapping[str, Any]] = None,
        plan_id: Optional[str] = None,
        peak_margin_evidence: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Open one roll. A second open roll on the same old contract is refused."""
        required = int(required_replacement_quantity or 0)
        if required <= 0:
            raise RollStateError(
                {"required_replacement_quantity": required, "message": "must be positive"}
            )
        existing = self.open_for(
            strategy_id=strategy_id, old_instrument_id=old_instrument_id
        )
        if existing is not None:
            raise RollDuplicate(
                {
                    "strategy_id": str(strategy_id),
                    "old_instrument_id": str(old_instrument_id),
                    "roll_id": existing["roll_id"],
                    "message": (
                        "A roll is already open on this contract; two would fight over "
                        "the same transition"
                    ),
                }
            )

        # The plan (when named) is the authority for the contracts this roll may
        # move AND for how much must be acquired: it must be THIS strategy's
        # frozen ACQUISITION plan, and its quantity wins over the caller's.
        if plan_id:
            approved = self._validate_plan_binding(
                strategy_id=str(strategy_id),
                account_id=str(account_id),
                plan_id=str(plan_id),
                old_instrument_id=str(old_instrument_id),
                new_instrument_id=str(new_instrument_id),
            )
            if int(required) != int(approved):
                raise RollPlanMismatch(
                    {
                        "plan_id": str(plan_id),
                        "required_replacement_quantity": int(required),
                        "plan_replacement_quantity": int(approved),
                        "message": (
                            "the linked plan's quantity is authoritative; a roll may not "
                            "require less than the approved acquisition"
                        ),
                    }
                )

        roll_id = str(uuid.uuid4())
        session = self.session_factory()
        try:
            # Serialize opens for this strategy: the duplicate check is a
            # read-then-write, and without the lock two concurrent evaluations could
            # both pass it and open rolls that then fight over one transition.
            if session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"roll-open:{strategy_id}:{old_instrument_id}"},
                )
                # Re-checked UNDER the lock, which is what makes the guard real.
                racing = self.open_for(
                    strategy_id=str(strategy_id), old_instrument_id=str(old_instrument_id)
                )
                if racing is not None:
                    session.rollback()
                    session.close()
                    raise RollDuplicate(
                        {
                            "strategy_id": str(strategy_id),
                            "old_instrument_id": str(old_instrument_id),
                            "roll_id": racing["roll_id"],
                            "message": "Another roll won the race for this contract",
                        }
                    )
            session.add(
                StrategyRoll(
                    roll_id=roll_id,
                    strategy_id=str(strategy_id),
                    account_id=str(account_id),
                    old_instrument_id=str(old_instrument_id),
                    new_instrument_id=str(new_instrument_id),
                    old_coordinate=dict(old_coordinate or {}),
                    new_coordinate=dict(new_coordinate or {}),
                    required_replacement_quantity=required,
                    proven_filled_quantity=0,
                    state="acquiring",
                    plan_id=plan_id,
                    peak_margin_evidence=(
                        dict(peak_margin_evidence) if peak_margin_evidence else None
                    ),
                )
            )
            self._record(
                session,
                roll_id=roll_id,
                event="created",
                detail={
                    "old_instrument_id": str(old_instrument_id),
                    "new_instrument_id": str(new_instrument_id),
                    "required_replacement_quantity": required,
                },
            )
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()
        return self.get(roll_id)

    def acquire(self, roll_id: str) -> Dict[str, Any]:
        """Step 1: the replacement leg is submitted first.

        Recording ``acquired`` is the state machine's acknowledgement that step 1
        happened; it does not itself place anything.
        """
        with self._transition(roll_id, allowed=("acquiring",), event="acquired") as row:
            row.state = "proving_filled"
        return self.get(roll_id)

    def replacement_fills(self, roll_id: str) -> List[Dict[str, Any]]:
        """This roll's recorded replacement executions, oldest first."""
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyRollEvent)
                .where(
                    StrategyRollEvent.roll_id == str(roll_id),
                    StrategyRollEvent.event == "replacement_filled",
                )
                .order_by(StrategyRollEvent.created_at)
            ).scalars().all()
            return [dict(row.detail or {}) for row in rows]

    def replacement_filled_quantity(self, roll_id: str) -> int:
        """The proven replacement quantity: the sum of this roll's own fills."""
        return int(
            sum(int(entry.get("quantity") or 0) for entry in self.replacement_fills(roll_id))
        )

    def record_replacement_fill(
        self,
        roll_id: str,
        *,
        paper_order_id: str,
        quantity: int,
        instrument_id: str,
        plan_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record ONE confirmed replacement execution against this roll.

        This is the roll's proof, and it is durable: the event names the paper
        order that filled and the quantity it filled. Because the proof is an
        event *of this roll*, a pre-existing holding on the new contract and an
        unrelated new-contract purchase can never be counted as replacement - they
        have no event here. Idempotent by ``paper_order_id``, so a retried or
        replayed execution records the same fill exactly once.
        """
        order_id = str(paper_order_id or "")
        amount = int(quantity or 0)
        if not order_id:
            raise RollStateError(
                {"roll_id": str(roll_id), "message": "a replacement fill needs its paper order"}
            )
        if amount <= 0:
            raise RollStateError(
                {
                    "roll_id": str(roll_id),
                    "paper_order_id": order_id,
                    "quantity": amount,
                    "message": "a replacement fill must be a positive executed quantity",
                }
            )
        session = self.session_factory()
        try:
            row = session.execute(
                select(StrategyRoll)
                .where(StrategyRoll.roll_id == str(roll_id))
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise RollStateError({"roll_id": str(roll_id), "message": "roll not found"})
            if str(instrument_id) != str(row.new_instrument_id):
                raise RollStateError(
                    {
                        "roll_id": str(roll_id),
                        "instrument_id": str(instrument_id),
                        "new_instrument_id": str(row.new_instrument_id),
                        "message": "only the replacement contract can be recorded as replacement",
                    }
                )
            if str(row.state) not in ("acquiring", "proving_filled", "action_required"):
                raise RollStateError(
                    {
                        "roll_id": str(roll_id),
                        "state": str(row.state),
                        "message": "this roll is no longer acquiring a replacement",
                    }
                )
            recorded = session.execute(
                select(StrategyRollEvent).where(
                    StrategyRollEvent.roll_id == str(roll_id),
                    StrategyRollEvent.event == "replacement_filled",
                )
            ).scalars().all()
            if any(
                str((entry.detail or {}).get("paper_order_id") or "") == order_id
                for entry in recorded
            ):
                # Idempotent replay: the same paper order is recorded once.
                session.rollback()
                return self.get(roll_id)
            self._record(
                session,
                roll_id=str(roll_id),
                event="replacement_filled",
                detail={
                    "paper_order_id": order_id,
                    "quantity": amount,
                    "instrument_id": str(instrument_id),
                    "plan_id": None if plan_id is None else str(plan_id),
                    "actor_id": None if actor_id is None else str(actor_id),
                },
            )
            row.proven_filled_quantity = int(
                sum(int((entry.detail or {}).get("quantity") or 0) for entry in recorded)
            ) + amount
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()
        return self.get(roll_id)

    def prove_filled(
        self, roll_id: str, *, proven_quantity: Optional[int] = None
    ) -> Dict[str, Any]:
        """Step 2: read the attributed book and decide whether the roll may proceed.

        Full proof moves the roll to ``releasing_old``. Anything less marks it
        ``action_required`` with the old attribution intact — and never reverses,
        because reversing would close a position the strategy still holds on the
        strength of a fill that did not happen.

        The proof is this roll's own recorded replacement executions
        (:meth:`record_replacement_fill`), never a caller's number and never the
        raw attributed book: the book also carries holdings that predate the roll
        and holdings that belong to other decisions, neither of which proves a
        replacement. ``proven_quantity`` remains an INTERNAL seam for the state
        machine's own unit tests and is never reachable from the HTTP surface.
        """
        roll = self.get(roll_id)
        if roll is None:
            raise RollStateError({"roll_id": str(roll_id), "message": "roll not found"})
        if str(roll["state"]) not in ("acquiring", "proving_filled", "action_required"):
            # Idempotent replay: a proof that already landed (a retried request,
            # or a restart) reports the standing decision instead of writing a
            # second one. Any other state is a real refusal.
            if (
                str(roll["state"]) in ("releasing_old", "completed")
                and self.replacement_filled_quantity(roll_id)
                >= int(roll["required_replacement_quantity"])
            ):
                return roll
            raise RollStateError(
                {"roll_id": str(roll_id), "state": str(roll["state"]),
                 "message": "only an in-flight roll can prove a fill"}
            )

        if proven_quantity is None:
            if self._positions_reader is not None:
                # INTERNAL test seam only: the HTTP surface constructs the machine
                # without a reader, so production proof is always this roll's own
                # recorded replacement executions. A synthetic proof reader stands
                # in for them in the machine's own unit tests.
                proven = self.attributed_quantity(
                    strategy_id=str(roll["strategy_id"]),
                    account_id=str(roll["account_id"]),
                    instrument_id=str(roll["new_instrument_id"]),
                    product=str(roll["new_coordinate"].get("product") or "NRML"),
                )
            else:
                proven = self.replacement_filled_quantity(roll_id)
        else:
            proven = int(proven_quantity)
        proven = abs(proven)

        required = int(roll["required_replacement_quantity"])
        if proven >= required:
            with self._transition(
                roll_id, allowed=("acquiring", "proving_filled", "action_required"),
                event="fill_proven",
                detail={"proven_filled_quantity": proven, "required": required},
            ) as row:
                row.proven_filled_quantity = proven
                row.state = "releasing_old"
                row.action_reason = None
            return self.get(roll_id)

        with self._transition(
            roll_id, allowed=("acquiring", "proving_filled", "action_required"),
            event="stalled",
            detail={
                "proven_filled_quantity": proven,
                "required": required,
                "shortfall": required - proven,
            },
        ) as row:
            row.proven_filled_quantity = proven
            row.state = "action_required"
            row.action_reason = "replacement_incomplete"
        return self.get(roll_id)

    def release_close(self, roll_id: str) -> Dict[str, Any]:
        """Step 3: release the old-contract close step — reachable only when proven.

        The guard is the state itself, not a separate boolean, so there is no path
        that reaches this step without the replacement having been proven full.
        """
        roll = self.get(roll_id)
        if roll is None:
            raise RollStateError({"roll_id": str(roll_id), "message": "roll not found"})
        if str(roll["state"]) != "releasing_old":
            raise ReleaseRefused(
                {
                    "roll_id": str(roll_id),
                    "state": str(roll["state"]),
                    "required_replacement_quantity": int(roll["required_replacement_quantity"]),
                    "proven_filled_quantity": int(roll["proven_filled_quantity"]),
                    "message": (
                        "The old-contract close step is released only after the FULL "
                        "required replacement quantity is proven filled."
                    ),
                }
            )
        with self._transition(
            roll_id, allowed=("releasing_old",), event="close_released"
        ) as row:
            row.action_reason = None
        return self.get(roll_id)

    def mark_old_flat(self, roll_id: str, *, old_quantity: Optional[int] = None) -> Dict[str, Any]:
        """Step 4: the old book must be PROVEN flat, not assumed flat."""
        roll = self.get(roll_id)
        if roll is None:
            raise RollStateError({"roll_id": str(roll_id), "message": "roll not found"})
        if str(roll["state"]) != "releasing_old":
            raise RollStateError(
                {"roll_id": str(roll_id), "state": str(roll["state"])}
            )
        measured = (
            int(old_quantity)
            if old_quantity is not None
            else self.attributed_quantity(
                strategy_id=str(roll["strategy_id"]),
                account_id=str(roll["account_id"]),
                instrument_id=str(roll["old_instrument_id"]),
                product=str(roll["old_coordinate"].get("product") or "NRML"),
            )
        )
        if measured != 0:
            # Retained attribution: the roll cannot complete on an assumption.
            raise RollNotFlat(
                {
                    "roll_id": str(roll_id),
                    "old_attributed_quantity": measured,
                    "message": "The old contract's book is not flat",
                }
            )
        with self._transition(
            roll_id, allowed=("releasing_old",), event="old_flat",
            detail={"old_attributed_quantity": 0},
        ) as row:
            row.state = "completed"
        self._record_only(roll_id, "completed", detail={"old_attributed_quantity": 0})
        return self.get(roll_id)

    def stall(self, roll_id: str, *, reason: str) -> Dict[str, Any]:
        """Flag an in-flight roll for the owner. Capacity and attribution stay put."""
        with self._transition(
            roll_id, allowed=OPEN_ROLL_STATES, event="stalled", detail={"reason": str(reason)}
        ) as row:
            row.state = "action_required"
            row.action_reason = str(reason)
        return self.get(roll_id)

    def escalate(self, roll_id: str, *, reason: str) -> Dict[str, Any]:
        """Notify the owner once, leaving the state alone.

        Escalation is not a transition: it reports that an in-flight roll needs a
        human, and it must not be the thing that changes the roll's state.
        """
        roll = self.get(roll_id)
        if roll is None:
            raise RollStateError({"roll_id": str(roll_id), "message": "roll not found"})
        sent = False
        if self._notifier is not None:
            try:
                sent = bool(self._notifier(str(roll["account_id"]), dict(roll)))
            except Exception:  # noqa: BLE001 - escalation never breaks the roll
                sent = False
        self._record_only(
            roll_id, "escalated", detail={"reason": str(reason), "notified": sent}
        )
        return {**self.get(roll_id), "escalated": sent}

    # -- internals ----------------------------------------------------------

    def _transition(
        self,
        roll_id: str,
        *,
        allowed: tuple,
        event: str,
        detail: Optional[Mapping[str, Any]] = None,
    ):
        return _RollTransition(
            self, roll_id, allowed=allowed, event=event, detail=detail or {}
        )

    @staticmethod
    def _record(
        session: Any,
        *,
        roll_id: str,
        event: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Append one event, stamped strictly after its predecessor.

        ``created_at`` is the trail's only ordering, and a whole roll can land
        inside one second — so without this the sequence would sort alphabetically
        and a reader could not reconstruct the transition the order encodes.
        """
        latest = session.execute(
            select(func.max(StrategyRollEvent.created_at)).where(
                StrategyRollEvent.roll_id == str(roll_id)
            )
        ).scalar()
        stamp = _utcnow()
        if isinstance(latest, datetime):
            previous = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            if previous >= stamp:
                stamp = previous + timedelta(microseconds=1)
        session.add(
            StrategyRollEvent(
                id=str(uuid.uuid4()),
                roll_id=str(roll_id),
                event=str(event),
                detail=dict(detail or {}),
                created_at=stamp,
            )
        )

    def _record_only(
        self, roll_id: str, event: str, *, detail: Optional[Mapping[str, Any]] = None
    ) -> None:
        with self.session_factory() as session:
            self._record(session, roll_id=roll_id, event=event, detail=detail)
            session.commit()

    @staticmethod
    def _view(row: StrategyRoll) -> Dict[str, Any]:
        return {
            "roll_id": str(row.roll_id),
            "strategy_id": str(row.strategy_id),
            "account_id": str(row.account_id),
            # Both identities, through the whole transition.
            "old_instrument_id": str(row.old_instrument_id),
            "new_instrument_id": str(row.new_instrument_id),
            "old_coordinate": dict(row.old_coordinate or {}),
            "new_coordinate": dict(row.new_coordinate or {}),
            "required_replacement_quantity": int(row.required_replacement_quantity or 0),
            "proven_filled_quantity": int(row.proven_filled_quantity or 0),
            "state": str(row.state),
            "action_reason": row.action_reason,
            "peak_margin_evidence": dict(row.peak_margin_evidence or {}),
            "plan_id": str(row.plan_id) if row.plan_id else None,
        }


class _RollTransition:
    """One guarded state change that always lands its event."""

    def __init__(
        self,
        machine: RollStateMachine,
        roll_id: str,
        *,
        allowed: tuple,
        event: str,
        detail: Mapping[str, Any],
    ) -> None:
        self.machine = machine
        self.roll_id = str(roll_id)
        self.allowed = allowed
        self.event = event
        self.detail = dict(detail)
        self.session = None
        self.row = None

    def __enter__(self) -> StrategyRoll:
        self.session = self.machine.session_factory()
        self.row = self.session.execute(
            select(StrategyRoll).where(StrategyRoll.roll_id == self.roll_id)
        ).scalar_one_or_none()
        if self.row is None:
            self.session.close()
            raise RollStateError({"roll_id": self.roll_id, "message": "roll not found"})
        if str(self.row.state) not in self.allowed:
            state = str(self.row.state)
            self.session.close()
            raise RollStateError(
                {"roll_id": self.roll_id, "state": state, "allowed": list(self.allowed)}
            )
        return self.row

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                self.session.rollback()
                return False
            self.machine._record(
                self.session, roll_id=self.roll_id, event=self.event, detail=self.detail
            )
            self.session.commit()
            return False
        finally:
            self.session.close()
