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
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Set, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    AccountIngestState,
    BrokerTradeFact,
    StrategyAttributionAdjustmentLine,
    StrategyPositionProjection,
    StrategyReconciliationState,
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

    # --------------------------------------------------------- adjustments

    def create_reclassification(self, **kwargs: Any) -> Dict[str, Any]:
        """Append-only owner reclassification, implemented with the attribution store.

        The adjustment tables are attribution state (they move quantity between
        the manual residual and a strategy book), so the single implementation
        lives beside the fold that consumes them; this delegates so callers have
        one account-truth surface.
        """
        from backend.strategies.attribution import SqlAttributionStore

        return SqlAttributionStore(session_factory=self.session_factory).create_reclassification(**kwargs)

    # --------------------------------------------------------- reconciliation

    def broker_quantities(self, *, account_id: str, db: Optional[Any] = None) -> Dict[Coordinate, int]:
        """The broker's own net per coordinate (the account's live book)."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                text(
                    """
                    SELECT instrument_token, exchange, tradingsymbol, product, net_quantity
                    FROM public.account_positions
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        except SQLAlchemyError:
            return {}
        finally:
            if owns_db:
                session.close()
        return {_coord(row): int(row[4] or 0) for row in rows}

    def attributed_quantities(self, *, account_id: str, db: Optional[Any] = None) -> Dict[Coordinate, int]:
        """Σ strategy-book quantities on the account, live environment only.

        Canonical and unresolved raw rows both count: their coordinates are
        explicit, and excluding unresolved exposure would understate what the
        platform believes it owns.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                select(
                    StrategyPositionProjection.instrument_token,
                    StrategyPositionProjection.exchange,
                    StrategyPositionProjection.tradingsymbol,
                    StrategyPositionProjection.product,
                    func.sum(StrategyPositionProjection.net_quantity),
                )
                .where(
                    StrategyPositionProjection.account_id == account_id,
                    StrategyPositionProjection.execution_environment == "live",
                )
                .group_by(
                    StrategyPositionProjection.instrument_token,
                    StrategyPositionProjection.exchange,
                    StrategyPositionProjection.tradingsymbol,
                    StrategyPositionProjection.product,
                )
            ).all()
        finally:
            if owns_db:
                session.close()
        return {_coord(row): int(row[4] or 0) for row in rows}

    def reconciliation_state(
        self, *, account_id: str, db: Optional[Any] = None
    ) -> Dict[Coordinate, Dict[str, Any]]:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                select(StrategyReconciliationState).where(
                    StrategyReconciliationState.account_id == account_id
                )
            ).scalars().all()
        finally:
            if owns_db:
                session.close()
        return {
            (
                int(row.instrument_token),
                str(row.exchange),
                str(row.tradingsymbol),
                str(row.product),
            ): {
                "divergence_class": str(row.divergence_class),
                "broker_quantity": int(row.broker_quantity),
                "attributed_quantity": int(row.attributed_quantity),
                "manual_quantity": int(row.manual_quantity),
                "residual_quantity": int(row.residual_quantity),
                "refresh_attempts": int(row.refresh_attempts or 0),
                "owner_notified_at": row.owner_notified_at,
                "last_checked_at": row.last_checked_at,
            }
            for row in rows
        }

    def upsert_reconciliation(
        self,
        *,
        account_id: str,
        coordinate: Coordinate,
        divergence_class: str,
        broker_quantity: int,
        attributed_quantity: int,
        manual_quantity: int,
        refresh_attempts: int,
        resolved: bool,
        owner_notified_at: Optional[datetime] = None,
        db: Optional[Any] = None,
    ) -> None:
        """Persist one coordinate's classification. Recomputed, never hand-set."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            residual = broker_quantity - attributed_quantity - manual_quantity
            now = _utcnow()
            row = session.execute(
                select(StrategyReconciliationState).where(
                    StrategyReconciliationState.account_id == account_id,
                    StrategyReconciliationState.instrument_token == coordinate[0],
                    StrategyReconciliationState.exchange == coordinate[1],
                    StrategyReconciliationState.tradingsymbol == coordinate[2],
                    StrategyReconciliationState.product == coordinate[3],
                )
            ).scalar_one_or_none()
            if row is None:
                row = StrategyReconciliationState(
                    account_id=account_id,
                    instrument_token=coordinate[0],
                    exchange=coordinate[1],
                    tradingsymbol=coordinate[2],
                    product=coordinate[3],
                )
                session.add(row)
            row.divergence_class = divergence_class
            row.broker_quantity = broker_quantity
            row.attributed_quantity = attributed_quantity
            row.manual_quantity = manual_quantity
            row.residual_quantity = residual
            row.refresh_attempts = refresh_attempts
            row.last_checked_at = now
            row.resolved_at = now if resolved else None
            if owner_notified_at is not None and row.owner_notified_at is None:
                # Escalation is stamped once and never cleared by later checks.
                row.owner_notified_at = owner_notified_at
            session.flush()
            if owns_db:
                session.commit()
        except SQLAlchemyError:
            # A database without the reconciliation table simply has no persisted
            # state; classification still returns its verdict to the caller.
            if owns_db:
                session.rollback()
        finally:
            if owns_db:
                session.close()

    def is_frozen_coordinate(self, *, account_id: str, coordinate: Coordinate) -> Optional[str]:
        """The divergence class freezing this coordinate, or ``None``.

        Both ``pending_ingest`` and ``unexplained`` freeze new exposure: an
        uncertain residual is never treated as harmless.
        """
        state = self.reconciliation_state(account_id=account_id).get(coordinate)
        if not state:
            return None
        divergence = str(state["divergence_class"])
        return divergence if divergence in (PENDING_INGEST, UNEXPLAINED) else None


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


class ReconciliationService:
    """Classify each account coordinate and freeze new exposure while uncertain.

    Classification is per coordinate and per D-2:

    * ``aligned`` — the identity ``broker == Σ attributed + manual`` holds.
    * ``pending_ingest`` — it does not hold, and bounded refresh may still find
      missing truth (attempts below the bound, or ingest not idle/complete yet).
    * ``unexplained`` — it still does not hold once the bound is exhausted and
      ingest is idle and complete.

    ``pending_ingest`` and ``unexplained`` both freeze **new exposure** on that
    coordinate; risk-reducing orders stay permitted, and only persistent
    ``unexplained`` escalates once to the account owner. Classification and the
    freeze never depend on notification success.
    """

    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_INTERVAL_SECONDS = 30

    def __init__(
        self,
        store: Optional[AccountTruthStore] = None,
        *,
        ingest_service: Optional["AccountTruthService"] = None,
        run_async: Optional[Callable[..., Any]] = None,
        notifier: Optional[Callable[[str, str, Coordinate, Dict[str, Any]], bool]] = None,
        max_attempts: Optional[int] = None,
    ) -> None:
        self.store = store or AccountTruthStore()
        self._ingest = ingest_service
        if run_async is None:
            run_async = asyncio.to_thread
        self._run_async = run_async
        self._notifier = notifier
        self.max_attempts = int(
            max_attempts
            if max_attempts is not None
            else (os.environ.get("RECONCILIATION_REFRESH_MAX_ATTEMPTS") or self.DEFAULT_MAX_ATTEMPTS)
        )

    async def reconcile_account(self, account_id: str) -> Dict[str, Any]:
        """Recompute one account's classification, freezing what is uncertain."""
        broker = await self._run_async(self.store.broker_quantities, account_id=account_id)
        attributed = await self._run_async(self.store.attributed_quantities, account_id=account_id)
        manual = await self._run_async(self.store.manual_residual_by_coordinate, account_id=account_id)
        previous = await self._run_async(self.store.reconciliation_state, account_id=account_id)
        ingest_state = await self._run_async(self.store.ingest_state, account_id=account_id)

        coordinates: Set[Coordinate] = set(broker) | set(attributed) | set(manual)
        results: List[Dict[str, Any]] = []
        for coordinate in sorted(coordinates):
            broker_quantity = int(broker.get(coordinate, 0))
            attributed_quantity = int(attributed.get(coordinate, 0))
            manual_quantity = int(manual.get(coordinate, 0))
            residual = broker_quantity - attributed_quantity - manual_quantity
            prior = previous.get(coordinate, {})
            attempts = int(prior.get("refresh_attempts") or 0)

            if residual != 0:
                attempts += 1
                # Bounded refresh: each uncertain check is one ingest attempt.
                if attempts <= self.max_attempts and self._ingest is not None:
                    await self._ingest.ingest_account(account_id)
                    ingest_state = await self._run_async(
                        self.store.ingest_state, account_id=account_id
                    )

            divergence = self._classify(
                residual=residual, attempts=attempts, ingest_state=ingest_state
            )
            if divergence == ALIGNED:
                attempts = 0

            notified_at = prior.get("owner_notified_at")
            if divergence == UNEXPLAINED and notified_at is None and self._notifier is not None:
                # Escalate at most once; a failed dispatch leaves the stamp unset
                # so a later check retries — classification never waits on it.
                if self._notifier(account_id, divergence, coordinate, {"residual_quantity": residual}):
                    notified_at = _utcnow()

            await self._run_async(
                self.store.upsert_reconciliation,
                account_id=account_id,
                coordinate=coordinate,
                divergence_class=divergence,
                broker_quantity=broker_quantity,
                attributed_quantity=attributed_quantity,
                manual_quantity=manual_quantity,
                refresh_attempts=attempts,
                resolved=divergence == ALIGNED,
                owner_notified_at=notified_at if divergence == UNEXPLAINED else None,
            )
            results.append(
                {
                    "coordinate": coordinate,
                    "divergence_class": divergence,
                    "broker_quantity": broker_quantity,
                    "attributed_quantity": attributed_quantity,
                    "manual_quantity": manual_quantity,
                    "residual_quantity": residual,
                    "refresh_attempts": attempts,
                    "owner_notified_at": notified_at,
                }
            )
        return {
            "account_id": account_id,
            "coordinates": results,
            "frozen": [row["coordinate"] for row in results if row["divergence_class"] != ALIGNED],
        }

    def _classify(self, *, residual: int, attempts: int, ingest_state: Dict[str, Any]) -> str:
        if residual == 0:
            return ALIGNED
        if attempts < self.max_attempts:
            return PENDING_INGEST
        # The bound is exhausted, but a cycle that has not completed cleanly means
        # missing truth is still plausible — pending, not unexplained.
        if str(ingest_state.get("status") or INGEST_IDLE) != INGEST_IDLE:
            return PENDING_INGEST
        return UNEXPLAINED


def reconciliation_refusal(
    *,
    account_id: str,
    coordinate: Coordinate,
    divergence_class: Optional[str],
    side: str,
    net_quantity: int,
) -> Optional[Dict[str, Any]]:
    """``None`` when the order may proceed; a named refusal otherwise.

    A frozen coordinate admits only risk-**reducing** orders: a SELL against a
    long net, or a BUY against a short net. Everything else would increase
    exposure while the book is not trustworthy. The refusal names the
    reconciliation state so a negative manual residual is explicit rather than
    mysterious (walkthrough 7 case 2).
    """
    if divergence_class is None or divergence_class == ALIGNED:
        return None
    normalized = str(side or "").upper()
    reducing = (net_quantity > 0 and normalized == "SELL") or (
        net_quantity < 0 and normalized == "BUY"
    )
    if reducing:
        return None
    return {
        "rejection_reason": "RECONCILIATION_FREEZE",
        "account_id": account_id,
        "coordinate": {
            "instrument_token": coordinate[0],
            "exchange": coordinate[1],
            "tradingsymbol": coordinate[2],
            "product": coordinate[3],
        },
        "divergence_class": divergence_class,
        "broker_net_quantity": net_quantity,
        "message": (
            "New exposure on this instrument is frozen while account truth is "
            f"reconciling ({divergence_class}); risk-reducing orders remain permitted."
        ),
    }


def coordinate_of(order: Dict[str, Any]) -> Coordinate:
    """The reconciliation coordinate an order acts on."""
    return (
        int(order.get("instrument_token") or 0),
        str(order.get("exchange") or ""),
        str(order.get("tradingsymbol") or ""),
        str(order.get("product") or ""),
    )


def unresolved_exit_detail(*, divergence_class: str, manual_quantity: int, broker_net: int) -> Dict[str, Any]:
    """Detail for the one-sided broker-net exit guard, naming reconciliation.

    The guard's arithmetic is unchanged — an exit sized to the strategy book can
    exceed the broker net when a manual residual has already reduced it. What
    changes is that the refusal now says *why*, instead of looking like a broker
    inconsistency.
    """
    return {
        "rejection_reason": "MANUAL_RESIDUAL_BLOCKS_FULL_EXIT",
        "divergence_class": divergence_class,
        "manual_quantity": manual_quantity,
        "broker_net_quantity": broker_net,
        "message": (
            "The account holds less than this strategy's attributed quantity because "
            f"{abs(manual_quantity)} is unattributed manual exposure "
            f"(reconciliation: {divergence_class}). Reduce the strategy's exit size, "
            "or reclassify the manual fill with an audited adjustment."
        ),
    }


def _default_owner_notifier(
    account_id: str, divergence: str, coordinate: Coordinate, detail: Dict[str, Any]
) -> bool:
    """Escalate once through the durable notification outbox.

    The recipient is the **account owner** — the app user who owns the canonical
    strategies on that account (``strategies.owner_id``). The hosted-strategy
    authorization resolver yields authorized account *scopes*, not identities, so
    the owning strategies are what actually name the person to notify. With no
    owning strategy there is no recipient: return ``False`` so the caller leaves
    ``owner_notified_at`` unset and retries later. Classification and the freeze
    never depend on this call succeeding.
    """
    try:
        from sqlalchemy import text as _text

        from backend.notifications.repository import SqlAlchemyNotificationRepository
        from backend.strategies.attribution import SqlAttributionStore

        store = SqlAttributionStore()
        session = store.session_factory()
        try:
            owners = [
                str(row[0])
                for row in session.execute(
                    _text(
                        "SELECT DISTINCT owner_id FROM strategies WHERE account_scope = :account_id"
                    ),
                    {"account_id": account_id},
                ).fetchall()
                if str(row[0] or "")
            ]
        finally:
            session.close()
        if not owners:
            return False

        repository = SqlAlchemyNotificationRepository()
        for owner in owners:
            repository.enqueue_run_notification(
                owner_id=owner,
                run_id=f"reconciliation:{account_id}",
                channel_names=_owner_channels(repository, owner),
                text=(
                    f"Account {account_id} has an unexplained divergence on "
                    f"{coordinate[1]}:{coordinate[2]} {coordinate[3]}: "
                    f"residual {detail.get('residual_quantity')}. New exposure is frozen "
                    "until this is reconciled."
                ),
                subject="Strategy reconciliation needs attention",
                idempotency_key=f"unexplained:{account_id}:{coordinate[1]}:{coordinate[2]}:{coordinate[3]}",
            )
        return True
    except Exception:
        # Notification failure must never block classification or the freeze.
        return False


def _owner_channels(repository: Any, owner_id: str) -> list:
    try:
        channels = repository.list_channels(owner_id) if hasattr(repository, "list_channels") else []
    except Exception:
        channels = []
    enabled = [
        str(channel.get("name") or channel.get("channel_name") or "")
        for channel in channels or []
        if str(channel.get("status") or "active") == "active"
    ]
    return [name for name in enabled if name]


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
