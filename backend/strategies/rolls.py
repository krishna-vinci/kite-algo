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

from sqlalchemy import func, select
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
    "fill_proven",
    "close_released",
    "old_flat",
    "completed",
    "stalled",
    "escalated",
)


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
            from backend.workflows.repository import SessionLocal

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

        roll_id = str(uuid.uuid4())
        session = self.session_factory()
        try:
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

    def prove_filled(
        self, roll_id: str, *, proven_quantity: Optional[int] = None
    ) -> Dict[str, Any]:
        """Step 2: read the attributed book and decide whether the roll may proceed.

        Full proof moves the roll to ``releasing_old``. Anything less marks it
        ``action_required`` with the old attribution intact — and never reverses,
        because reversing would close a position the strategy still holds on the
        strength of a fill that did not happen.
        """
        roll = self.get(roll_id)
        if roll is None:
            raise RollStateError({"roll_id": str(roll_id), "message": "roll not found"})
        if str(roll["state"]) not in ("acquiring", "proving_filled", "action_required"):
            raise RollStateError(
                {"roll_id": str(roll_id), "state": str(roll["state"]),
                 "message": "only an in-flight roll can prove a fill"}
            )

        expected_sign = 1 if str(roll["new_coordinate"].get("side") or "BUY").upper() != "SELL" else -1
        if proven_quantity is None:
            proven = self.attributed_quantity(
                strategy_id=str(roll["strategy_id"]),
                account_id=str(roll["account_id"]),
                instrument_id=str(roll["new_instrument_id"]),
                product=str(roll["new_coordinate"].get("product") or "NRML"),
            )
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
        _ = expected_sign
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
