"""The durable reservation ledger: atomic capacity claims and a real lifecycle.

Two properties carry the whole design (R3 §8):

* **First durable reservation wins.** The capacity check and the reservation
  insert happen in ONE transaction holding a PostgreSQL advisory lock keyed on the
  account, so two concurrent plans cannot both spend the same capacity. The loser
  is refused ``CAPACITY_EXCEEDED`` — never queued and never resized, because
  deterministic resizing is a non-goal and a queued claim would mean a plan whose
  authority was granted before its capacity existed.

* **Capital backing an open position is never released because its evaluation
  expired.** ``consumed`` is a terminal state that no expiry, release or owner
  action touches, and the owner cannot steal capacity from active execution: once
  a reservation has recorded verified progress it can only move forward.

The reservation row is mutable (it has a lifecycle); its truth is the append-only
event log beside it, which records every transition with the actor who caused it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    StrategyReservation,
    StrategyReservationEvent,
)

#: Statuses that still hold capacity against the allocation.
HOLDING_STATUSES = ("active", "renewed", "action_required")

#: Terminal states. ``consumed`` is terminal in the strongest sense: it represents
#: real exposure, so nothing may release it.
TERMINAL_STATUSES = ("consumed", "released", "expired")

#: The plan's step has not begun. Only these may be released outright.
UNSTARTED_STATUSES = ("active", "renewed")

RESERVATION_EVENTS = (
    "created",
    "renewed",
    "advanced",
    "consumed",
    "released",
    "expired",
    "action_required",
    "disposition_confirmed",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class ReservationError(Exception):
    """A refusal from the ledger, always carrying a named reason."""

    reason_code = "RESERVATION_ERROR"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


class CapacityExceeded(ReservationError):
    reason_code = "CAPACITY_EXCEEDED"


class ReservationNotFound(ReservationError):
    reason_code = "RESERVATION_NOT_FOUND"


class ReservationStateError(ReservationError):
    reason_code = "RESERVATION_STATE_INVALID"


class ReleaseForbidden(ReservationError):
    """No API may release capacity backing active or consumed exposure."""

    reason_code = "RELEASE_FORBIDDEN"


class DispositionUnproven(ReservationError):
    """V1 has no proved execution state, so disposition cannot be confirmed."""

    reason_code = "DISPOSITION_UNPROVEN"


@dataclass(frozen=True)
class ClaimRequest:
    plan_id: str
    strategy_id: str
    account_id: str
    evaluation_id: str
    execution_environment: str
    requirement_inr: float
    valid_until: datetime
    allocation_inr: Optional[float] = None
    margin_evidence: Optional[Dict[str, Any]] = None
    margin_as_of: Optional[datetime] = None
    actor_id: Optional[str] = None


class ReservationLedger:
    """Claims, transitions and reads. Every transition writes an event."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # -- locking ------------------------------------------------------------

    @staticmethod
    def _lock_account(session: Any, account_id: str) -> None:
        """Serialize capacity claims for one account. PostgreSQL only; SQLite no-op.

        ``pg_advisory_xact_lock`` is transaction-scoped, so the lock is held from
        here until commit — strictly before the capacity sum is read, which is what
        makes the claim atomic rather than merely optimistic.
        """
        if session.bind.dialect.name != "postgresql":
            return
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"admission:{account_id}"},
        )

    # -- capacity -----------------------------------------------------------

    def held_notional(self, *, account_id: str, db: Optional[Any] = None) -> float:
        """Capacity currently held on the account, including consumed exposure."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                select(StrategyReservation.reserved_notional_inr).where(
                    StrategyReservation.account_id == str(account_id),
                    StrategyReservation.status.in_(HOLDING_STATUSES + ("consumed",)),
                )
            ).scalars().all()
            return float(sum(float(value or 0) for value in rows))
        finally:
            if owns_db:
                session.close()

    def available_capacity(
        self, *, account_id: str, allocation_inr: Optional[float], db: Optional[Any] = None
    ) -> Optional[float]:
        if allocation_inr is None:
            return None
        return float(allocation_inr) - self.held_notional(account_id=account_id, db=db)

    # -- claim --------------------------------------------------------------

    def claim(self, request: ClaimRequest, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Admit-and-claim in ONE transaction. First durable reservation wins."""
        moment = now or _utcnow()
        session = self.session_factory()
        try:
            self._lock_account(session, request.account_id)

            existing = session.execute(
                select(StrategyReservation).where(
                    StrategyReservation.plan_id == str(request.plan_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                # One plan claims capacity once, ever: a retry returns the original
                # claim rather than extending or duplicating it.
                return self._view(existing)

            if request.allocation_inr is not None:
                held = self.held_notional(account_id=request.account_id, db=session)
                if held + float(request.requirement_inr) > float(request.allocation_inr):
                    raise CapacityExceeded(
                        {
                            "account_id": request.account_id,
                            "strategy_id": request.strategy_id,
                            "allocation_inr": float(request.allocation_inr),
                            "held_inr": held,
                            "requested_inr": float(request.requirement_inr),
                            "message": (
                                "Capacity is already committed; the first durable reservation wins "
                                "and this plan is refused rather than queued or resized."
                            ),
                        }
                    )

            reservation_id = str(uuid.uuid4())
            row = StrategyReservation(
                reservation_id=reservation_id,
                plan_id=str(request.plan_id),
                strategy_id=str(request.strategy_id),
                account_id=str(request.account_id),
                evaluation_id=str(request.evaluation_id),
                execution_environment=str(request.execution_environment),
                status="active",
                reserved_notional_inr=float(request.requirement_inr),
                margin_evidence=dict(request.margin_evidence or {}) or None,
                margin_as_of=request.margin_as_of,
                valid_until=request.valid_until,
            )
            session.add(row)
            session.flush()
            self._record(
                session,
                reservation_id=reservation_id,
                event="created",
                actor_id=request.actor_id,
                detail={
                    "reserved_notional_inr": float(request.requirement_inr),
                    "valid_until": request.valid_until.isoformat(),
                },
                at=moment,
            )
            session.commit()
            return self._view(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- lifecycle ----------------------------------------------------------

    def renew(
        self,
        reservation_id: str,
        *,
        actor_id: Optional[str] = None,
        extend_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Extend an active reservation's validity and record the event.

        Renewal happens only on verified progress (R3 §8). In Phase 4 the plan's
        execution state does not exist yet, so the caller supplies the progress
        evidence and the ledger records the transition; the verification itself
        arrives with the execution phases.
        """
        moment = now or _utcnow()
        with self._transition(
            reservation_id,
            allowed=UNSTARTED_STATUSES,
            event="renewed",
            now=moment,
            actor_id=actor_id,
        ) as row:
            row.status = "renewed"
            row.renewed_at = moment
            base = _as_datetime(row.valid_until) or moment
            window = int(extend_seconds if extend_seconds is not None else 900)
            row.valid_until = max(base, moment) + timedelta(seconds=window)
        return self.get(reservation_id)

    def advance(
        self, reservation_id: str, *, actor_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Record verified progress. From here the reservation is NOT unstarted.

        This is the boundary that makes "the owner cannot steal capacity from
        active execution" enforceable: after progress, release is forbidden and
        the only forward path is consumption.
        """
        moment = now or _utcnow()
        with self._transition(
            reservation_id,
            allowed=UNSTARTED_STATUSES,
            event="advanced",
            now=moment,
            actor_id=actor_id,
        ) as row:
            row.status = "renewed"
            row.renewed_at = moment
        return self.get(reservation_id)

    def consume(
        self, reservation_id: str, *, actor_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Filled exposure stops being a reservation and becomes attributed exposure.

        ``consumed`` is terminal: no expiry, release or owner action may take the
        capacity back, because it now backs a real position.
        """
        moment = now or _utcnow()
        with self._transition(
            reservation_id,
            allowed=("active", "renewed", "action_required"),
            event="consumed",
            now=moment,
            actor_id=actor_id,
        ) as row:
            row.status = "consumed"
        return self.get(reservation_id)

    def release(
        self,
        reservation_id: str,
        *,
        reason: str = "terminal_unfilled",
        actor_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Release a terminal-unfilled reservation's unused capacity.

        Refused once the reservation has recorded verified progress or been
        consumed: capacity backing active execution is not the owner's to reclaim.
        """
        moment = now or _utcnow()
        session = self.session_factory()
        try:
            self._lock_account(session, str(self._required(session, reservation_id).account_id))
            row = self._required(session, reservation_id)
            if str(row.status) == "consumed" or self._has_event(session, reservation_id, "advanced"):
                raise ReleaseForbidden(
                    {
                        "reservation_id": str(reservation_id),
                        "status": str(row.status),
                        "message": (
                            "This reservation backs active or consumed exposure; no API may "
                            "release that capacity."
                        ),
                    }
                )
            if str(row.status) not in UNSTARTED_STATUSES:
                raise ReservationStateError(
                    {"reservation_id": str(reservation_id), "status": str(row.status)}
                )
            row.status = "released"
            row.released_at = moment
            row.release_reason = str(reason)
            self._record(
                session,
                reservation_id=str(reservation_id),
                event="released",
                actor_id=actor_id,
                detail={"reason": str(reason)},
                at=moment,
            )
            session.commit()
            return self._view(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def expire(
        self, reservation_id: str, *, actor_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Expire an unstarted reservation whose validity has lapsed.

        Consumed capacity never expires: the evaluation's authority ends, the
        position it created does not.
        """
        moment = now or _utcnow()
        session = self.session_factory()
        try:
            row = self._required(session, reservation_id)
            if str(row.status) == "consumed":
                raise ReservationStateError(
                    {
                        "reservation_id": str(reservation_id),
                        "status": "consumed",
                        "message": "Consumed capacity is never released by expiry.",
                    }
                )
            if str(row.status) not in UNSTARTED_STATUSES:
                raise ReservationStateError(
                    {"reservation_id": str(reservation_id), "status": str(row.status)}
                )
            valid_until = _as_datetime(row.valid_until)
            if valid_until is not None and moment < valid_until:
                raise ReservationStateError(
                    {
                        "reservation_id": str(reservation_id),
                        "status": str(row.status),
                        "valid_until": valid_until.isoformat(),
                        "message": "Reservation validity has not lapsed.",
                    }
                )
            row.status = "expired"
            row.released_at = moment
            row.release_reason = "validity_lapsed"
            self._record(
                session,
                reservation_id=str(reservation_id),
                event="expired",
                actor_id=actor_id,
                detail={},
                at=moment,
            )
            session.commit()
            return self._view(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def require_action(
        self, reservation_id: str, *, actor_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Flag an unresolved executing plan. Capacity stays held, deliberately."""
        moment = now or _utcnow()
        with self._transition(
            reservation_id,
            allowed=("active", "renewed"),
            event="action_required",
            now=moment,
            actor_id=actor_id,
        ) as row:
            row.status = "action_required"
        return self.get(reservation_id)

    def confirm_disposition(
        self, reservation_id: str, *, actor_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Refuse in V1: there is no proved execution state to confirm against.

        ``action_required`` releases unused capacity only after the execution state
        is proven AND the account owner confirms the disposition. Neither exists
        yet, so this refuses rather than releasing capacity on an unproven guess.
        """
        raise DispositionUnproven(
            {
                "reservation_id": str(reservation_id),
                "message": (
                    "Disposition cannot be confirmed in V1: no proved execution state exists, "
                    "so unused capacity stays held."
                ),
            }
        )

    # -- reads --------------------------------------------------------------

    def get(self, reservation_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyReservation).where(
                    StrategyReservation.reservation_id == str(reservation_id)
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def for_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyReservation).where(
                    StrategyReservation.plan_id == str(plan_id)
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def list_for_strategy(self, *, strategy_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyReservation)
                .where(StrategyReservation.strategy_id == str(strategy_id))
                .order_by(StrategyReservation.created_at.desc())
                .limit(int(limit))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def events(self, reservation_id: str) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyReservationEvent)
                .where(StrategyReservationEvent.reservation_id == str(reservation_id))
                .order_by(
                    StrategyReservationEvent.created_at, StrategyReservationEvent.event
                )
            ).scalars().all()
            return [
                {
                    "event": str(row.event),
                    "actor_id": row.actor_id,
                    "detail": dict(row.detail or {}),
                }
                for row in rows
            ]

    # -- internals ----------------------------------------------------------

    def _transition(
        self,
        reservation_id: str,
        *,
        allowed: tuple,
        event: str,
        now: datetime,
        actor_id: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ):
        return _Transition(
            self,
            reservation_id,
            allowed=allowed,
            event=event,
            now=now,
            actor_id=actor_id,
            detail=detail,
        )

    def _required(self, session: Any, reservation_id: str) -> StrategyReservation:
        row = session.execute(
            select(StrategyReservation).where(
                StrategyReservation.reservation_id == str(reservation_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ReservationNotFound({"reservation_id": str(reservation_id)})
        return row

    @staticmethod
    def _has_event(session: Any, reservation_id: str, event: str) -> bool:
        return (
            session.execute(
                select(StrategyReservationEvent.id).where(
                    StrategyReservationEvent.reservation_id == str(reservation_id),
                    StrategyReservationEvent.event == str(event),
                )
            ).first()
            is not None
        )

    @staticmethod
    def _record(
        session: Any,
        *,
        reservation_id: str,
        event: str,
        actor_id: Optional[str],
        detail: Mapping[str, Any],
        at: datetime,
    ) -> None:
        # Strictly increasing per reservation: created_at is the only ordering the
        # schema carries, and a caller may legitimately supply one `now` for a
        # sequence of transitions.
        latest = session.execute(
            select(func.max(StrategyReservationEvent.created_at)).where(
                StrategyReservationEvent.reservation_id == str(reservation_id)
            )
        ).scalar()
        stamp = at
        latest_dt = _as_datetime(latest)
        if latest_dt is not None and latest_dt >= stamp:
            stamp = latest_dt + timedelta(microseconds=1)
        session.add(
            StrategyReservationEvent(
                id=str(uuid.uuid4()),
                reservation_id=str(reservation_id),
                event=str(event),
                actor_id=actor_id,
                detail=dict(detail or {}),
                created_at=stamp,
            )
        )

    @staticmethod
    def _iso(value: Any) -> Optional[str]:
        """Normalize to an offset-aware ISO string: SQLite drops tzinfo on write."""
        parsed = _as_datetime(value)
        return parsed.isoformat() if parsed is not None else None

    @classmethod
    def _view(cls, row: StrategyReservation) -> Dict[str, Any]:
        return {
            "reservation_id": str(row.reservation_id),
            "plan_id": str(row.plan_id),
            "strategy_id": str(row.strategy_id),
            "account_id": str(row.account_id),
            "evaluation_id": str(row.evaluation_id),
            "execution_environment": str(row.execution_environment),
            "status": str(row.status),
            "reserved_notional_inr": float(row.reserved_notional_inr or 0),
            "margin_evidence": dict(row.margin_evidence or {}),
            "margin_as_of": cls._iso(row.margin_as_of),
            "valid_until": cls._iso(row.valid_until),
            "renewed_at": cls._iso(row.renewed_at),
            "released_at": cls._iso(row.released_at),
            "release_reason": row.release_reason,
        }


class _Transition:
    """A single guarded state change that always lands its event."""

    def __init__(
        self,
        ledger: ReservationLedger,
        reservation_id: str,
        *,
        allowed: tuple,
        event: str,
        now: datetime,
        actor_id: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ):
        self.ledger = ledger
        self.reservation_id = str(reservation_id)
        self.allowed = allowed
        self.event = event
        self.now = now
        self.actor_id = actor_id
        self.detail = dict(detail or {})
        self.session = None
        self.row = None

    def __enter__(self) -> StrategyReservation:
        self.session = self.ledger.session_factory()
        self.row = self.ledger._required(self.session, self.reservation_id)
        if str(self.row.status) not in self.allowed:
            status = str(self.row.status)
            self.session.close()
            raise ReservationStateError(
                {"reservation_id": self.reservation_id, "status": status, "allowed": list(self.allowed)}
            )
        return self.row

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                self.session.rollback()
                return False
            self.ledger._record(
                self.session,
                reservation_id=self.reservation_id,
                event=self.event,
                actor_id=self.actor_id,
                detail=self.detail,
                at=self.now,
            )
            self.session.commit()
            return False
        finally:
            self.session.close()
