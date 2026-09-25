"""Paper partial fills: fill progress and tranche planning (G12).

Until now every paper fill was instant and full, which makes a rebalance look
cleaner than it is: real orders fill across several trades, leave remainders open,
and force the executor to decide what "progress" means. This module owns that
meaning.

Two decisions worth stating, because they are what make the rest honest:

* **Progress lives in its own table.** An order with no progress row behaves
  exactly as it did before, so the existing runtime is untouched and a rollback
  that drops the table leaves instant-full fills rather than a broken one.

* **An open remainder is in-flight, not settled.** A partially filled order has
  not finished changing the book, so anything that treats it as done — a barrier,
  a reservation release — would be asserting a flatness the account does not have.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import PaperOrderFillProgress

#: Progress statuses (mirrors ``ck_pofp_status``).
PROGRESS_STATUSES = ("open", "partially_filled", "filled", "cancelled")

#: Opt-in. 1.0 = instant-full, which is the pre-existing behaviour and the
#: documented rollback state; set PAPER_PARTIAL_FILL_RATIO below 1 to enable
#: tranche fills. Enabling it by default would silently change execution
#: semantics for every existing paper strategy.
DEFAULT_PARTIAL_FILL_RATIO = 1.0

#: Statuses that still hold an unexecuted remainder.
OPEN_PROGRESS_STATUSES = ("open", "partially_filled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def partial_fill_ratio() -> float:
    """The configured fraction of the remainder filled per attempt.

    Defaults to ``1.0`` (instant-full). Partial fills are a deliberate opt-in
    because they change what an execution *means* — an order that used to be
    finished is now in flight — so a deployment chooses them rather than
    inheriting them from an upgrade.
    """
    raw = os.environ.get("PAPER_PARTIAL_FILL_RATIO")
    if raw is None:
        return DEFAULT_PARTIAL_FILL_RATIO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PARTIAL_FILL_RATIO
    if value <= 0:
        return DEFAULT_PARTIAL_FILL_RATIO
    return min(value, 1.0)


def next_tranche(remaining: int, ratio: float) -> int:
    """How much of the remainder this attempt fills.

    Deliberately generous (``ceil``, at least one unit): a ratio that rounds the
    tranche down to zero would leave an order open forever while claiming to be
    making progress, which is the one outcome worse than filling late.
    """
    remaining = int(remaining)
    if remaining <= 0:
        return 0
    if ratio >= 1.0:
        return remaining
    return max(1, min(remaining, int(math.ceil(remaining * ratio))))


@dataclass(frozen=True)
class FillProgress:
    account_scope: str
    paper_order_id: str
    order_quantity: int
    filled_quantity: int
    remaining_quantity: int
    status: str

    @property
    def is_complete(self) -> bool:
        return self.remaining_quantity <= 0 or self.status == "filled"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_PROGRESS_STATUSES


class PaperFillProgressStore:
    """Reads and writes ``paper_order_fill_progress``. Never touches orders."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    def progress_for(self, *, account_scope: str, paper_order_id: str) -> Optional[FillProgress]:
        with self.session_factory() as session:
            row = session.execute(
                select(PaperOrderFillProgress).where(
                    PaperOrderFillProgress.account_scope == str(account_scope),
                    PaperOrderFillProgress.paper_order_id == str(paper_order_id),
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._view(row)

    def start(self, *, account_scope: str, paper_order_id: str, quantity: int) -> FillProgress:
        """Create the progress row for an order, or return the existing one."""
        existing = self.progress_for(
            account_scope=account_scope, paper_order_id=paper_order_id
        )
        if existing is not None:
            return existing
        session = self.session_factory()
        try:
            session.add(
                PaperOrderFillProgress(
                    account_scope=str(account_scope),
                    paper_order_id=str(paper_order_id),
                    filled_quantity=0,
                    remaining_quantity=int(quantity),
                    status="open",
                )
            )
            session.commit()
        except IntegrityError:
            # Another writer created it first; theirs is the truth.
            session.rollback()
        finally:
            session.close()
        return self.progress_for(
            account_scope=account_scope, paper_order_id=paper_order_id
        )

    def record_fill(
        self, *, account_scope: str, paper_order_id: str, filled_quantity: int, quantity: int
    ) -> FillProgress:
        """Advance the recorded progress after a tranche filled."""
        session = self.session_factory()
        try:
            row = session.execute(
                select(PaperOrderFillProgress).where(
                    PaperOrderFillProgress.account_scope == str(account_scope),
                    PaperOrderFillProgress.paper_order_id == str(paper_order_id),
                )
            ).scalar_one_or_none()
            if row is None:
                row = PaperOrderFillProgress(
                    account_scope=str(account_scope),
                    paper_order_id=str(paper_order_id),
                    filled_quantity=0,
                    remaining_quantity=int(quantity),
                    status="open",
                )
                session.add(row)
            row.filled_quantity = int(row.filled_quantity or 0) + int(filled_quantity)
            row.remaining_quantity = max(int(quantity) - int(row.filled_quantity), 0)
            row.status = "filled" if row.remaining_quantity == 0 else "partially_filled"
            row.updated_at = _utcnow()
            session.commit()
            return self._view(row)
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    def cancel(self, *, account_scope: str, paper_order_id: str) -> Optional[FillProgress]:
        """Cancel the unexecuted remainder: terminal status AND zero remaining.

        The remainder is what a cancel removes, so leaving it non-zero would make
        the row claim an order that still has work to do. The filled quantity is
        never touched - a cancel preserves what already filled.
        """
        session = self.session_factory()
        try:
            row = session.execute(
                select(PaperOrderFillProgress).where(
                    PaperOrderFillProgress.account_scope == str(account_scope),
                    PaperOrderFillProgress.paper_order_id == str(paper_order_id),
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "cancelled"
            row.remaining_quantity = 0
            row.updated_at = _utcnow()
            session.commit()
            return self._view(row)
        finally:
            session.close()

    def open_remainder_for(
        self, *, account_scope: Optional[str] = None, paper_order_ids: Optional[list] = None
    ) -> list:
        """Orders with an unexecuted remainder: the barrier's in-flight set."""
        with self.session_factory() as session:
            query = select(PaperOrderFillProgress).where(
                PaperOrderFillProgress.status.in_(OPEN_PROGRESS_STATUSES),
                PaperOrderFillProgress.remaining_quantity > 0,
            )
            if account_scope:
                query = query.where(
                    PaperOrderFillProgress.account_scope == str(account_scope)
                )
            if paper_order_ids is not None:
                query = query.where(
                    PaperOrderFillProgress.paper_order_id.in_(
                        [str(item) for item in paper_order_ids]
                    )
                )
            rows = session.execute(query).scalars().all()
            return [self._view(row) for row in rows]

    def has_open_remainder(
        self, *, account_scope: Optional[str] = None, paper_order_ids: Optional[list] = None
    ) -> bool:
        """Whether anything in flight has not finished changing the book."""
        return bool(self.open_remainder_for(account_scope=account_scope, paper_order_ids=paper_order_ids))

    @staticmethod
    def _view(row: PaperOrderFillProgress) -> FillProgress:
        filled = int(row.filled_quantity or 0)
        remaining = int(row.remaining_quantity or 0)
        return FillProgress(
            account_scope=str(row.account_scope),
            paper_order_id=str(row.paper_order_id),
            order_quantity=filled + remaining,
            filled_quantity=filled,
            remaining_quantity=remaining,
            status=str(row.status),
        )
