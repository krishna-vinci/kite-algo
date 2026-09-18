"""MIS square-off: the platform's timing, the platform's evidence (D-2, D-3).

The square-off machinery already exists and is deliberately **not** changed here:
the per-product schedule, the ``mis_squareoff_buffer`` rule and the durable exit
claim path are reused as they are. What this module adds is the two things that
were missing:

* the **record** — a square-off that fired is evidence, not an assertion, and a
  square-off that failed must be distinguishable from one that never ran;
* the **sizing rule** — an exit is sized to the strategy's *attributed* quantity
  and never beyond it, which is what stops one strategy's square-off selling
  another strategy's shares (R3 §12 walkthrough 2, case 1).

The outcome vocabulary carries the distinction the architecture cares about. A
failed square-off is ``action_required`` and keeps reconciling; it is explicitly
NOT settlement, whose four axes stay unsatisfied. A broker auto-square-off
observed afterwards is ``missed_by_broker`` — a fallback that happened, never the
control that decided, because the platform owning the timing is the whole point.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import StrategySquareoffEvidence

#: Outcomes (mirrors ``ck_sse_outcome``).
SQUAREOFF_OUTCOMES = (
    "squared_off",
    "action_required",
    "missed_by_broker",
    "stale_worker_exit",
)

#: Outcomes that mean the platform's square-off did NOT complete. These keep
#: reconciling and are never settlement.
UNRESOLVED_OUTCOMES = ("action_required", "missed_by_broker")

#: The per-product defaults, as verified in source (R3 §16). Reused verbatim from
#: the protection runtime's own schedule so the two can never drift.
DEFAULT_SQUAREOFF_SCHEDULE = {
    "NSE:MIS": "15:20",
    "BSE:MIS": "15:20",
    "NFO:MIS": "15:25",
    "CDS:MIS": "16:45",
    "MCX:MIS": "23:20",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def squareoff_schedule() -> Dict[str, str]:
    """The live schedule, override included.

    Delegates to the protection runtime's own resolver rather than duplicating the
    defaults here: two copies of a schedule is two chances to disagree about when
    the platform squares off, and the one that would be wrong is the one nobody
    checked.
    """
    from backend.app.background import _worker_protection_squareoff_schedule

    return dict(_worker_protection_squareoff_schedule())


def scheduled_time_for(exchange: str, product: str = "MIS") -> Optional[str]:
    """The square-off time for one exchange/product, or ``None`` if unscheduled."""
    key = f"{str(exchange or '').upper()}:{str(product or '').upper()}"
    return squareoff_schedule().get(key)


def attributed_exit_size(*, attributed_quantity: int, requested_quantity: int) -> int:
    """Clamp an exit to the strategy's attributed quantity, in the exit's direction.

    An exit closes a book, so its direction is OPPOSITE the book's: flattening a
    long (+100) is a sell (-N), flattening a short (-100) is a buy-back (+N). An
    order in the same direction as the book would GROW it, which is not an exit at
    all and is refused rather than clamped.

    One-sided on purpose. A square-off that asks for more than the strategy owns
    would sell somebody else's shares — another strategy's book, or the manual
    residual — so the clamp is not a safety margin, it is the isolation guarantee
    of R3 §12 case 1: reducing is always permitted, growing past what is owned
    never is.
    """
    attributed = int(attributed_quantity or 0)
    requested = int(requested_quantity or 0)
    if attributed == 0 or requested == 0:
        return 0
    if attributed > 0:
        # A long book is exited by selling.
        return -min(abs(requested), attributed) if requested < 0 else 0
    # A short book is exited by buying back.
    return min(abs(requested), abs(attributed)) if requested > 0 else 0


@dataclass(frozen=True)
class SquareoffRecord:
    account_id: str
    strategy_id: str
    strategy_run_id: str
    product: str
    session_date: date
    exchange: str
    scheduled_at: datetime
    outcome: str
    exit_claim_id: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None

    @property
    def resolved(self) -> bool:
        return self.outcome not in UNRESOLVED_OUTCOMES


class MisSquareoffEvidenceStore:
    """Append-only evidence. It records what happened; it decides nothing."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    def record(self, entry: SquareoffRecord) -> Dict[str, Any]:
        if entry.outcome not in SQUAREOFF_OUTCOMES:
            raise ValueError(f"unknown square-off outcome: {entry.outcome}")
        row = StrategySquareoffEvidence(
            id=str(uuid.uuid4()),
            account_id=str(entry.account_id),
            strategy_id=str(entry.strategy_id),
            strategy_run_id=str(entry.strategy_run_id),
            product=str(entry.product),
            session_date=entry.session_date,
            exchange=str(entry.exchange),
            scheduled_at=entry.scheduled_at,
            exit_claim_id=entry.exit_claim_id,
            outcome=str(entry.outcome),
            detail=dict(entry.detail or {}),
        )
        with self.session_factory() as session:
            session.add(row)
            session.flush()
            # Built inside the session: committing expires the instance, and a
            # detached row cannot answer for its own columns.
            view = self._view(row)
            session.commit()
        return view

    def for_run(self, *, strategy_run_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategySquareoffEvidence)
                .where(StrategySquareoffEvidence.strategy_run_id == str(strategy_run_id))
                .order_by(
                    StrategySquareoffEvidence.session_date.desc(),
                    StrategySquareoffEvidence.created_at.desc(),
                )
                .limit(int(limit))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def for_strategy(self, *, strategy_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategySquareoffEvidence)
                .where(StrategySquareoffEvidence.strategy_id == str(strategy_id))
                .order_by(
                    StrategySquareoffEvidence.session_date.desc(),
                    StrategySquareoffEvidence.created_at.desc(),
                )
                .limit(int(limit))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def unresolved_for_run(self, *, strategy_run_id: str) -> List[Dict[str, Any]]:
        """Square-offs that did not complete — these keep reconciling."""
        return [
            row
            for row in self.for_run(strategy_run_id=strategy_run_id)
            if row["outcome"] in UNRESOLVED_OUTCOMES
        ]

    @staticmethod
    def _view(row: StrategySquareoffEvidence) -> Dict[str, Any]:
        return {
            "id": str(row.id),
            "account_id": str(row.account_id),
            "strategy_id": str(row.strategy_id),
            "strategy_run_id": str(row.strategy_run_id),
            "product": str(row.product),
            "session_date": row.session_date.isoformat() if row.session_date else None,
            "exchange": str(row.exchange),
            "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
            "exit_claim_id": row.exit_claim_id,
            "outcome": str(row.outcome),
            "detail": dict(row.detail or {}),
        }
