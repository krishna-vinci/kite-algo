"""Account-wide fill truth, the manual book, reconciliation and the freeze.

Why this module exists
----------------------

The broker exposes ONE net position per ``(instrument, product)`` and knows
nothing about which strategy opened it. G1 gave each strategy its own
attributable book; this module supplies the two things that book cannot answer
alone:

1. **Account-wide ingested truth.** Every fill on the account — from a platform
   strategy, a webhook, or a human tapping "sell" in the broker app — is
   persisted as an insert-only fact keyed by durable broker identity
   ``(account_id, trade_id)``. Nothing is ever rewritten or deleted, so a bad
   ingest generation is corrected by ingesting the missing truth.

2. **The manual book as a derived residual.** The manual quantity for a
   coordinate is

       Σ signed unlinked ingested facts − Σ adjustment-line deltas

   and deliberately **not** ``broker − attributed``: subtracting from the broker
   net would silently absorb missing ingestion, which is exactly the condition
   ``pending_ingest`` exists to expose (R3 §17, D-1). Before any adjustment
   exists this reduces to the unlinked-facts sum.

The accounting identity this module serves is, per coordinate,

    broker_quantity == Σ attributed (strategy books) + manual_quantity

and it is **quantity/exposure only** — no cash, tax-lot or cost-basis semantics
(R3 §10; G15 stays post-V1).

Divergence classification is R3 §17's: ``aligned`` when the identity holds,
``pending_ingest`` while bounded refresh may still find missing truth, and
``unexplained`` once that bound is exhausted. Both non-aligned classes freeze new
exposure on the affected coordinate; risk-reducing exits stay permitted, and only
persistent ``unexplained`` escalates to the account owner. Account flatness never
substitutes for strategy flatness, and no heuristic ever assigns a fill to a
strategy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Set, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    AccountIngestState,
    BrokerTradeFact,
    StrategyAttributionAdjustmentLine,
)

#: A quantity coordinate: the broker's own position key minus the account.
Coordinate = Tuple[int, str, str, str]

#: Ingest states (mirrors ``ck_ais_status``).
INGEST_IDLE = "idle"
INGEST_REFRESHING = "refreshing"
INGEST_STALE = "stale"

#: Divergence classes (mirrors ``ck_srs_divergence_class``).
ALIGNED = "aligned"
PENDING_INGEST = "pending_ingest"
UNEXPLAINED = "unexplained"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coord(row: Any) -> Coordinate:
    return (int(row[0]), str(row[1]), str(row[2]), str(row[3]))


def _signed(transaction_type: Any, quantity: Any) -> int:
    qty = int(quantity or 0)
    return qty if str(transaction_type or "").upper() == "BUY" else -qty


class AccountTruthStore:
    """Persisted account truth: ingested facts, ingest state, manual residual.

    New tables are written and read through the ORM on the shared ``Base``
    (portable to the SQLite test database); pre-existing platform tables are read
    through the codebase's ``public.``-qualified Core SQL.
    """

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # ------------------------------------------------------------ ingest state

    def ingest_state(self, *, account_id: str, db: Optional[Any] = None) -> Dict[str, Any]:
        """The account's ingest cursor. A missing row reads as the initial idle state."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                select(AccountIngestState).where(AccountIngestState.account_id == account_id)
            ).scalar_one_or_none()
            if row is None:
                return {
                    "account_id": account_id,
                    "ingest_generation": 0,
                    "status": INGEST_IDLE,
                    "last_orders_fetch_at": None,
                    "last_complete_ingest_at": None,
                }
            return {
                "account_id": str(row.account_id),
                "ingest_generation": int(row.ingest_generation or 0),
                "status": str(row.status or INGEST_IDLE),
                "last_orders_fetch_at": row.last_orders_fetch_at,
                "last_complete_ingest_at": row.last_complete_ingest_at,
            }
        finally:
            if owns_db:
                session.close()

    def begin_ingest(self, *, account_id: str, db: Optional[Any] = None) -> int:
        """Open a new ingest generation and mark the account ``refreshing``."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                select(AccountIngestState).where(AccountIngestState.account_id == account_id)
            ).scalar_one_or_none()
            now = _utcnow()
            if row is None:
                row = AccountIngestState(
                    account_id=account_id,
                    ingest_generation=1,
                    status=INGEST_REFRESHING,
                    last_orders_fetch_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.ingest_generation = int(row.ingest_generation or 0) + 1
                row.status = INGEST_REFRESHING
                row.last_orders_fetch_at = now
                row.updated_at = now
            session.flush()
            generation = int(row.ingest_generation)
            if owns_db:
                session.commit()
            return generation
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def complete_ingest(self, *, account_id: str, generation: int, db: Optional[Any] = None) -> None:
        """Close a successful ingest: idle, and complete up to this generation."""
        self._set_ingest_status(
            account_id=account_id,
            status=INGEST_IDLE,
            generation=generation,
            complete=True,
            db=db,
        )

    def fail_ingest(self, *, account_id: str, generation: int, db: Optional[Any] = None) -> None:
        """A failed cycle leaves the state ``stale`` — never silently complete."""
        self._set_ingest_status(
            account_id=account_id,
            status=INGEST_STALE,
            generation=generation,
            complete=False,
            db=db,
        )

    def _set_ingest_status(
        self,
        *,
        account_id: str,
        status: str,
        generation: int,
        complete: bool,
        db: Optional[Any],
    ) -> None:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                select(AccountIngestState).where(AccountIngestState.account_id == account_id)
            ).scalar_one_or_none()
            now = _utcnow()
            if row is None:
                row = AccountIngestState(
                    account_id=account_id, ingest_generation=generation, status=status, updated_at=now
                )
                session.add(row)
            else:
                row.status = status
                row.updated_at = now
                if complete:
                    row.last_complete_ingest_at = now
                    row.ingest_generation = max(int(row.ingest_generation or 0), int(generation))
            session.flush()
            if owns_db:
                session.commit()
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    # ---------------------------------------------------------------- facts

    def ingest_trades(
        self,
        *,
        account_id: str,
        trades: Sequence[Dict[str, Any]],
        generation: Optional[int] = None,
        db: Optional[Any] = None,
    ) -> Dict[str, int]:
        """Persist broker trades as account truth. Insert-only; dedupe by identity.

        Returns ``{"inserted", "skipped"}``. Skipping an already-known
        ``(account_id, trade_id)`` is the normal steady state of a periodic
        cycle, not an error.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            effective_generation = (
                int(generation)
                if generation is not None
                else int(self.ingest_state(account_id=account_id, db=session)["ingest_generation"])
            )
            existing: Set[str] = {
                str(value)
                for value in session.execute(
                    select(BrokerTradeFact.trade_id).where(BrokerTradeFact.account_id == account_id)
                ).scalars()
            }
            inserted = 0
            skipped = 0
            for trade in trades:
                trade_id = str(trade.get("trade_id") or "").strip()
                if not trade_id:
                    continue
                if trade_id in existing:
                    skipped += 1
                    continue
                quantity = int(trade.get("quantity") or 0)
                if quantity <= 0:
                    skipped += 1
                    continue
                order_id = str(trade.get("order_id") or "").strip()
                if not order_id:
                    skipped += 1
                    continue
                session.add(
                    BrokerTradeFact(
                        fact_id=f"btf_{account_id}_{trade_id}",
                        account_id=account_id,
                        trade_id=trade_id,
                        broker_order_id=order_id,
                        instrument_token=int(trade.get("instrument_token") or 0),
                        exchange=str(trade.get("exchange") or ""),
                        tradingsymbol=str(trade.get("tradingsymbol") or ""),
                        product=str(trade.get("product") or ""),
                        transaction_type=str(trade.get("transaction_type") or "BUY").upper(),
                        quantity=quantity,
                        fill_price=_optional_float(
                            trade.get("average_price") or trade.get("price")
                        ),
                        trade_timestamp=_parse_timestamp(
                            trade.get("fill_timestamp")
                            or trade.get("exchange_timestamp")
                            or trade.get("order_timestamp")
                        ),
                        ingest_generation=effective_generation,
                    )
                )
                existing.add(trade_id)
                inserted += 1
            session.flush()
            if owns_db:
                session.commit()
            return {"inserted": inserted, "skipped": skipped}
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def fact_count(self, *, account_id: str, db: Optional[Any] = None) -> int:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(BrokerTradeFact)
                    .where(BrokerTradeFact.account_id == account_id)
                ).scalar()
                or 0
            )
        finally:
            if owns_db:
                session.close()

    # ------------------------------------------------------- manual residual

    def manual_residual_by_coordinate(
        self, *, account_id: str, db: Optional[Any] = None
    ) -> Dict[Coordinate, int]:
        """The manual book: signed unlinked facts minus adjustment-line deltas.

        Never ``broker − attributed``. A coordinate whose fills are all linked
        and has no adjustments reports ``0`` (present, so callers can see it is
        clean rather than merely unknown).
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            facts = session.execute(
                select(
                    BrokerTradeFact.instrument_token,
                    BrokerTradeFact.exchange,
                    BrokerTradeFact.tradingsymbol,
                    BrokerTradeFact.product,
                    BrokerTradeFact.broker_order_id,
                    BrokerTradeFact.transaction_type,
                    BrokerTradeFact.quantity,
                ).where(BrokerTradeFact.account_id == account_id)
            ).all()
            linked_orders = self._linked_order_ids(session, account_id=account_id)
            adjustments = session.execute(
                select(
                    StrategyAttributionAdjustmentLine.instrument_token,
                    StrategyAttributionAdjustmentLine.exchange,
                    StrategyAttributionAdjustmentLine.tradingsymbol,
                    StrategyAttributionAdjustmentLine.product,
                    StrategyAttributionAdjustmentLine.quantity_delta,
                ).where(StrategyAttributionAdjustmentLine.account_id == account_id)
            ).all()
        finally:
            if owns_db:
                session.close()

        residual: Dict[Coordinate, int] = {}
        for row in facts:
            values = list(row)
            coord = _coord(values)
            residual.setdefault(coord, 0)
            if str(values[4] or "") in linked_orders:
                continue
            residual[coord] += _signed(values[5], values[6])
        for row in adjustments:
            values = list(row)
            coord = _coord(values)
            residual.setdefault(coord, 0)
            # A line's delta is the quantity credited to the strategy, so the
            # manual residual moves the opposite way by construction.
            residual[coord] -= int(values[4] or 0)
        return residual

    @staticmethod
    def _linked_order_ids(session: Any, *, account_id: str) -> Set[str]:
        """Order ids a platform execution link claims for this account.

        Order-level attribution is what makes a fact *tracked*; a missing link
        table means nothing is claimed, so every fact is manual — which is the
        honest reading, not a silent zero.
        """
        try:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT broker_order_id
                    FROM public.worker_live_execution_links
                    WHERE account_id = :account_id
                      AND COALESCE(NULLIF(broker_order_id, ''), '') <> ''
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        except SQLAlchemyError:
            return set()
        return {str(row[0]) for row in rows}


class AccountTruthService:
    """Bounded, per-account-isolated ingestion.

    One account's broker error must never stall the others: each account is
    ingesting independently and a failure leaves that account's ingest state
    ``stale`` (so divergence classification can see the truth is not fresh)
    rather than pretending the cycle completed.
    """

    def __init__(
        self,
        store: Optional[AccountTruthStore] = None,
        *,
        trades_provider: Optional[Callable[[str], Any]] = None,
        run_async: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store or AccountTruthStore()
        self._trades_provider = trades_provider
        if run_async is None:
            run_async = asyncio.to_thread
        self._run_async = run_async

    async def ingest_account(self, account_id: str) -> Dict[str, Any]:
        """Run one ingest cycle for one account, never raising to the caller."""
        generation = await self._run_async(self.store.begin_ingest, account_id=account_id)
        try:
            trades = await self._trades(account_id)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            await self._run_async(
                self.store.fail_ingest, account_id=account_id, generation=generation
            )
            return {
                "account_id": account_id,
                "error": type(exc).__name__,
                "detail": str(exc),
                "generation": generation,
            }
        try:
            counts = await self._run_async(
                self.store.ingest_trades,
                account_id=account_id,
                trades=trades,
                generation=generation,
            )
            await self._run_async(
                self.store.complete_ingest, account_id=account_id, generation=generation
            )
            return {
                "account_id": account_id,
                "generation": generation,
                "inserted": counts["inserted"],
                "skipped": counts["skipped"],
            }
        except Exception as exc:  # noqa: BLE001
            await self._run_async(
                self.store.fail_ingest, account_id=account_id, generation=generation
            )
            return {
                "account_id": account_id,
                "error": type(exc).__name__,
                "detail": str(exc),
                "generation": generation,
            }

    async def ingest_accounts(self, account_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Ingest several accounts, isolating each failure from the others."""
        results: Dict[str, Dict[str, Any]] = {}
        for account_id in account_ids:
            results[str(account_id)] = await self.ingest_account(str(account_id))
        return results

    async def _trades(self, account_id: str) -> Sequence[Dict[str, Any]]:
        if self._trades_provider is None:
            return await self._run_async(_default_trades_provider, account_id)
        provided = self._trades_provider(account_id)
        if asyncio.iscoroutine(provided):
            provided = await provided
        return list(provided or [])


def _default_trades_provider(account_id: str) -> Sequence[Dict[str, Any]]:
    """Read the account's broker-wide trade book.

    ``kite.trades()`` is the account-wide trade book, which is exactly the truth
    being ingested: it covers platform-tracked and untracked fills alike, so the
    cycle does not need to walk orders and risk missing a fill whose order it
    never tracked.
    """
    from backend.api.routers.worker_shared import _load_live_kite_for_account
    from backend.broker_api.orders import OrdersService

    kite = _load_live_kite_for_account(account_id)
    service = OrdersService()
    trades = service.trades(kite, f"account-truth-ingest-{account_id}")
    return [trade.model_dump(mode="json") for trade in trades]


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
