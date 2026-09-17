"""The durable execution-quiescence barrier (G7, plan D-1/D-2).

Why this module exists
----------------------

Settlement — releasing attribution, claims or reconciliation blocks — needs
proof that no execution work is in flight. The platform's failure mode to avoid
is *inference*: a quiet window, two identical reads, or an account-level flat
position prove nothing, because an order admitted before the reads can still
complete after them. So quiescence is a **durable proof** tied to a version:

* every registered work transition (``work_created`` / ``work_resolved``)
  inserts an append-only event row and bumps ``barrier_version`` in the SAME
  transaction;
* a quiescence proof is ONE transaction holding the book's advisory lock that
  enumerates in-flight work (D-2), finds it EMPTY, records a
  ``proof_recorded`` event at the CURRENT version — proofs never bump the
  version — and stamps ``quiet_since_version = barrier_version``;
* any later work event bumps the version, which invalidates every prior proof
  by plain arithmetic (``quiet_since_version <> barrier_version``). Nothing is
  ever "still valid because nothing seemed to happen".

The in-flight enumeration is deterministic and **fail-closed**: any evidence
source that cannot be read (missing table, query failure, missing row
context) fails the proof as ``evidence_unavailable`` — an empty result set is
never manufactured from an error.

This module is the shared foundation (R3 §16, §23 Foundation C). Domain
adapters interpret the axes it produces; they never duplicate the barrier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    AccountIngestState,
    StrategyExecutionBarrier,
    StrategyExecutionBarrierEvent,
    StrategyPositionProjection,
    StrategyReconciliationState,
    StrategyRunBinding,
)

__all__ = [
    "BARRIER_EVENT_PROOF",
    "BARRIER_EVENT_WORK_CREATED",
    "BARRIER_EVENT_WORK_RESOLVED",
    "ExecutionBarrier",
    "InflightItem",
    "ProofResult",
    "SettlementEvidenceUnavailable",
    "enumerate_inflight_work",
]

#: Work vocabulary (the events that bump the version; mirrors ``ck_sebe_event``).
BARRIER_EVENT_WORK_CREATED = "work_created"
BARRIER_EVENT_WORK_RESOLVED = "work_resolved"
BARRIER_EVENT_PROOF = "proof_recorded"

#: Only these two transitions are *work*; the proof event never bumps.
WORK_EVENTS = (BARRIER_EVENT_WORK_CREATED, BARRIER_EVENT_WORK_RESOLVED)

#: Terminal broker order outcomes (the ``order_state_projection`` vocabulary the
#: recovery guards already use — a status outside this set is not proof of
#: anything and keeps the work in flight).
TERMINAL_ORDER_STATUSES = ("COMPLETE", "CANCELLED", "REJECTED", "LAPSED")

#: Intent statuses that mean the intent itself can never become work.
#: Anything else (unknown included) stays in flight: fail closed.
TERMINAL_INTENT_STATUSES = ("failed",)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SettlementEvidenceUnavailable(RuntimeError):
    """Evidence a proof needs could not be read — the proof FAILS (D-2).

    ``source`` names the unavailable evidence so the refusal is auditable. This
    is deliberately an ordinary failure result, not an exception that escapes:
    an unavailable source must degrade the proof to failure, never to empty.
    """

    def __init__(self, source: str) -> None:
        self.source = str(source)
        super().__init__(f"settlement evidence unavailable: {self.source}")


@dataclass(frozen=True)
class InflightItem:
    """One concrete piece of in-flight work, named so it can be acted on."""

    kind: str
    ref: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "detail": dict(self.detail)}


@dataclass(frozen=True)
class ProofResult:
    """The outcome of one proof attempt. A failed proof records NOTHING."""

    recorded: bool
    reason: str
    barrier_version: int
    quiet_since_version: Optional[int] = None
    inflight: List[InflightItem] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)


def _guarded(source: str):
    """Turn a query failure into ``SettlementEvidenceUnavailable(source)``.

    Context manager form so every enumeration query states, at the call site,
    which evidence it is a source of.
    """
    return _EvidenceGuard(source)


class _EvidenceGuard:
    def __init__(self, source: str) -> None:
        self.source = source

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and issubclass(exc_type, SQLAlchemyError):
            raise SettlementEvidenceUnavailable(self.source) from exc
        return False


def _bound_run_ids(db: Any, *, account_id: str, strategy_id: str, execution_environment: str) -> Set[str]:
    """Runs bound to this book. Unreadable bindings are unavailable evidence."""
    with _guarded("strategy_run_bindings"):
        rows = db.execute(
            select(StrategyRunBinding.strategy_run_id).where(
                StrategyRunBinding.account_id == account_id,
                StrategyRunBinding.strategy_id == strategy_id,
                StrategyRunBinding.execution_environment == execution_environment,
            )
        ).scalars().all()
    return {str(row) for row in rows}


def _run_execution_inflight(db: Any, *, account_id: str, run_id: str, items: List[InflightItem]) -> None:
    """(a)+(c) attributed orders non-terminal and links with unresolved execution.

    Scoped to ONE bound run; the checks reuse the ``has_unresolved_execution_for_run``
    logic exactly (trade-linked fills netting non-zero; an order link with a
    missing or non-terminal ``order_state_projection`` row), generalized from a
    boolean to the concrete items.
    """
    # (c-i) trade-linked fills whose net is non-zero: fills exist whose order
    # lifecycle never resolved cleanly for this run.
    with _guarded(f"execution_links:{run_id}"):
        net_row = db.execute(
            text(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN UPPER(COALESCE(otf.transaction_type, '')) = 'BUY' THEN otf.quantity
                        WHEN UPPER(COALESCE(otf.transaction_type, '')) = 'SELL' THEN -otf.quantity
                        ELSE 0
                    END
                ), 0) AS net_quantity
                FROM public.worker_live_execution_links wl
                INNER JOIN public.order_trade_fills otf
                  ON otf.account_id = wl.account_id
                 AND otf.trade_id = wl.trade_id
                WHERE wl.strategy_run_id = :strategy_run_id
                  AND wl.account_id = :account_id
                  AND wl.trade_id IS NOT NULL
                """
            ),
            {"strategy_run_id": run_id, "account_id": account_id},
        ).scalar()
    net_quantity = int(net_row or 0)
    if net_quantity != 0:
        items.append(
            InflightItem(
                "execution_link_unresolved",
                run_id,
                {"net_quantity": net_quantity},
            )
        )

    # (a) order-level links whose broker outcome is missing or non-terminal.
    with _guarded(f"order_state:{run_id}"):
        rows = db.execute(
            text(
                """
                SELECT wl.broker_order_id, COALESCE(osp.latest_status, '') AS latest_status
                FROM public.worker_live_execution_links wl
                LEFT JOIN public.order_state_projection osp
                  ON osp.account_id = wl.account_id
                 AND osp.order_id = wl.broker_order_id
                WHERE wl.strategy_run_id = :strategy_run_id
                  AND wl.account_id = :account_id
                  AND wl.trade_id IS NULL
                  AND wl.broker_order_id IS NOT NULL
                  AND (
                    osp.order_id IS NULL
                    OR UPPER(COALESCE(osp.latest_status, '')) NOT IN
                       ('COMPLETE', 'CANCELLED', 'REJECTED', 'LAPSED')
                  )
                """
            ),
            {"strategy_run_id": run_id, "account_id": account_id},
        ).fetchall()
    for row in rows:
        items.append(
            InflightItem(
                "order_non_terminal",
                f"{run_id}:{str(row[0])}",
                {"latest_status": str(row[1] or "")},
            )
        )


def _intent_inflight(db: Any, *, account_id: str, bound_run_ids: Set[str], items: List[InflightItem]) -> None:
    """(b) placed ``live_order_intents`` without a terminal broker outcome."""
    if not bound_run_ids:
        return
    postgres = db.bind.dialect.name == "postgresql"
    if postgres:
        run_predicate = "li.strategy_run_id = ANY(:run_ids)"
        params: Dict[str, Any] = {"account_id": account_id, "run_ids": sorted(bound_run_ids)}
        statement = text(
            f"""
            SELECT li.intent_id, COALESCE(li.status, '') AS status,
                   li.broker_order_id, COALESCE(osp.latest_status, '') AS latest_status
            FROM public.live_order_intents li
            LEFT JOIN public.order_state_projection osp
              ON osp.account_id = li.account_id
             AND osp.order_id = li.broker_order_id
            WHERE li.account_id = :account_id
              AND {run_predicate}
              AND UPPER(COALESCE(li.status, '')) NOT IN ('FAILED')
              AND (
                li.broker_order_id IS NULL
                OR osp.order_id IS NULL
                OR UPPER(COALESCE(osp.latest_status, '')) NOT IN
                   ('COMPLETE', 'CANCELLED', 'REJECTED', 'LAPSED')
              )
            """
        )
    else:
        from sqlalchemy import bindparam

        params = {"account_id": account_id}
        statement = text(
            """
            SELECT li.intent_id, COALESCE(li.status, '') AS status,
                   li.broker_order_id, COALESCE(osp.latest_status, '') AS latest_status
            FROM public.live_order_intents li
            LEFT JOIN public.order_state_projection osp
              ON osp.account_id = li.account_id
             AND osp.order_id = li.broker_order_id
            WHERE li.account_id = :account_id
              AND li.strategy_run_id IN :run_ids
              AND UPPER(COALESCE(li.status, '')) NOT IN ('FAILED')
              AND (
                li.broker_order_id IS NULL
                OR osp.order_id IS NULL
                OR UPPER(COALESCE(osp.latest_status, '')) NOT IN
                   ('COMPLETE', 'CANCELLED', 'REJECTED', 'LAPSED')
              )
            """
        ).bindparams(bindparam("run_ids", value=sorted(bound_run_ids), expanding=True))
    with _guarded("live_order_intents"):
        rows = db.execute(statement, params).fetchall()
    for row in rows:
        items.append(
            InflightItem(
                "intent_unresolved",
                str(row[0]),
                {"status": str(row[1] or ""), "latest_status": str(row[3] or "")},
            )
        )


def _reconciliation_inflight(
    db: Any, *, account_id: str, strategy_id: str, execution_environment: str, items: List[InflightItem]
) -> None:
    """(d) non-aligned reconciliation coordinates for THIS book's coordinates.

    ``pending_ingest`` and ``unexplained`` are in-flight truth. The coordinate
    set is the strategy's own book — another strategy's divergence on a
    coordinate this book never touched is that book's in-flight truth, not
    this one's.
    """
    with _guarded("strategy_position_projection"):
        coords = db.execute(
            select(
                StrategyPositionProjection.instrument_token,
                StrategyPositionProjection.exchange,
                StrategyPositionProjection.tradingsymbol,
                StrategyPositionProjection.product,
            ).where(
                StrategyPositionProjection.account_id == account_id,
                StrategyPositionProjection.strategy_id == strategy_id,
                StrategyPositionProjection.execution_environment == execution_environment,
            )
        ).all()
    if not coords:
        return
    book_coordinates = {
        (int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in coords
    }
    with _guarded("strategy_reconciliation_state"):
        rows = db.execute(
            select(StrategyReconciliationState).where(
                StrategyReconciliationState.account_id == account_id
            )
        ).scalars().all()
    for row in rows:
        coordinate = (
            int(row.instrument_token),
            str(row.exchange),
            str(row.tradingsymbol),
            str(row.product),
        )
        if coordinate not in book_coordinates:
            continue
        if str(row.divergence_class) == "aligned":
            continue
        items.append(
            InflightItem(
                "reconciliation_non_aligned",
                ":".join(str(part) for part in coordinate),
                {"divergence_class": str(row.divergence_class)},
            )
        )


def _ingest_inflight(db: Any, *, account_id: str, items: List[InflightItem]) -> None:
    """(e) an account ingest cycle still refreshing is unresolved truth."""
    with _guarded("account_ingest_state"):
        row = db.execute(
            select(AccountIngestState).where(AccountIngestState.account_id == account_id)
        ).scalar_one_or_none()
    if row is not None and str(row.status or "") == "refreshing":
        items.append(InflightItem("ingest_refreshing", account_id, {"status": "refreshing"}))


def enumerate_inflight_work(
    *, account_id: str, strategy_id: str, execution_environment: str, db: Any
) -> List[InflightItem]:
    """The D-2 union, evaluated on the caller's transaction.

    MUST be called inside the proof transaction (after the book's advisory
    lock). Deterministic: items are ordered by ``(kind, ref)`` so identical
    states produce byte-identical enumerations. Fail-closed: every source is
    guarded, and an unreadable source raises
    :class:`SettlementEvidenceUnavailable` — never an empty list.
    """
    items: List[InflightItem] = []
    bound = _bound_run_ids(
        db, account_id=account_id, strategy_id=strategy_id, execution_environment=execution_environment
    )
    for run_id in sorted(bound):
        _run_execution_inflight(db, account_id=account_id, run_id=run_id, items=items)
    _intent_inflight(db, account_id=account_id, bound_run_ids=bound, items=items)
    _reconciliation_inflight(
        db,
        account_id=account_id,
        strategy_id=strategy_id,
        execution_environment=execution_environment,
        items=items,
    )
    _ingest_inflight(db, account_id=account_id, items=items)
    items.sort(key=lambda item: (item.kind, item.ref))
    return items


class ExecutionBarrier:
    """The durable barrier per ``(account, strategy, execution_environment)``.

    New-table writes go through the ORM on the shared ``Base`` (portable to the
    SQLite test database); the version bump takes the database's atomic upsert
    path on PostgreSQL so two concurrent work transitions can never both claim
    the same version.
    """

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # ------------------------------------------------------------- work events

    def record_work_event(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        event: str,
        ref: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        db: Optional[Any] = None,
    ) -> int:
        """Record one work transition: bump the version and append the event.

        ONE transaction: the version can never disagree with the event log that
        describes it. Returns the NEW (bumped) version.
        """
        if event not in WORK_EVENTS:
            raise ValueError(f"event must be one of {', '.join(WORK_EVENTS)}: {event!r}")
        owns_db = db is None
        session = db or self.session_factory()
        try:
            version = self._bump_version(session, account_id, strategy_id, execution_environment)
            session.add(
                StrategyExecutionBarrierEvent(
                    id=str(uuid.uuid4()),
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                    version=int(version),
                    event=event,
                    ref=ref,
                    detail=dict(detail or {}),
                )
            )
            session.flush()
            if owns_db:
                session.commit()
            return int(version)
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def _bump_version(self, session: Any, account_id: str, strategy_id: str, execution_environment: str) -> int:
        """Atomically create-or-increment the barrier's version, in-tx."""
        if session.bind.dialect.name == "postgresql":
            row = session.execute(
                text(
                    """
                    INSERT INTO strategy_execution_barriers
                        (account_id, strategy_id, execution_environment, barrier_version, updated_at)
                    VALUES (:account_id, :strategy_id, :execution_environment, 1, NOW())
                    ON CONFLICT (account_id, strategy_id, execution_environment)
                    DO UPDATE SET
                        barrier_version = strategy_execution_barriers.barrier_version + 1,
                        updated_at = NOW()
                    RETURNING barrier_version
                    """
                ),
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "execution_environment": execution_environment,
                },
            ).fetchone()
            return int(row[0])

        # Read-modify-write on the test dialect: single-threaded there; the
        # concurrent-bump race is proved on PostgreSQL where the atomic upsert runs.
        row = self._load_barrier(session, account_id, strategy_id, execution_environment)
        if row is None:
            session.add(
                StrategyExecutionBarrier(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                    barrier_version=1,
                    updated_at=_utcnow(),
                )
            )
            session.flush()
            return 1
        row.barrier_version = int(row.barrier_version or 0) + 1
        row.updated_at = _utcnow()
        session.flush()
        return int(row.barrier_version)

    # ------------------------------------------------------------------ proofs

    def record_proof(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        ref: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        db: Optional[Any] = None,
    ) -> ProofResult:
        """Prove quiescence for one book, or fail recording nothing (D-1).

        One transaction: take the book's advisory lock FIRST (strictly before
        the in-flight snapshot), enumerate, and only on an EMPTY enumeration
        append the ``proof_recorded`` event at the CURRENT version (no bump)
        and stamp ``quiet_since_version = barrier_version``. Any later work
        event invalidates the proof by version arithmetic alone.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            self._lock_barrier(session, account_id, strategy_id, execution_environment)
            current = self._load_barrier(session, account_id, strategy_id, execution_environment)
            current_version = int(current.barrier_version or 0) if current is not None else 0

            try:
                inflight = enumerate_inflight_work(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                    db=session,
                )
            except SettlementEvidenceUnavailable as exc:
                if owns_db:
                    session.rollback()
                return ProofResult(
                    recorded=False,
                    reason="evidence_unavailable",
                    barrier_version=current_version,
                    unavailable=[exc.source],
                )

            if inflight:
                # Nothing has been written; release the lock cleanly when we own
                # the transaction. The failure names the work that is in flight.
                if owns_db:
                    session.rollback()
                return ProofResult(
                    recorded=False,
                    reason="inflight_work_present",
                    barrier_version=current_version,
                    inflight=inflight,
                )

            now = _utcnow()
            session.add(
                StrategyExecutionBarrierEvent(
                    id=str(uuid.uuid4()),
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                    version=current_version,
                    event=BARRIER_EVENT_PROOF,
                    ref=ref,
                    detail=dict(detail or {}),
                )
            )
            row = current
            if row is None:
                row = StrategyExecutionBarrier(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                    barrier_version=0,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.updated_at = now
            row.quiet_since_version = current_version
            row.last_proof_at = now
            session.flush()
            if owns_db:
                session.commit()
            return ProofResult(
                recorded=True,
                reason="quiescent",
                barrier_version=current_version,
                quiet_since_version=current_version,
            )
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    @staticmethod
    def _lock_barrier(session: Any, account_id: str, strategy_id: str, execution_environment: str) -> None:
        """Serialize proofs for one book. PostgreSQL only; SQLite no-op.

        The key is namespaced ``barrier:`` so a proof and a G1 projection
        recompute on the same book serialize against each other rather than
        sharing a lock slot with unrelated operations.
        """
        if session.bind.dialect.name != "postgresql":
            return
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"barrier:{account_id}:{strategy_id}:{execution_environment}"},
        )

    # ------------------------------------------------------------------- reads

    def state(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        db: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """The barrier's current state, including proof validity by version.

        ``proof_valid`` is the whole D-1 invariant in one comparison: a proof
        is valid exactly while ``quiet_since_version == barrier_version``. No
        timer, no quiet window, no repeated-read heuristic.
        """
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = self._load_barrier(session, account_id, strategy_id, execution_environment)
        finally:
            if owns_db:
                session.close()
        if row is None:
            return {
                "exists": False,
                "barrier_version": 0,
                "quiet_since_version": None,
                "last_proof_at": None,
                "proof_valid": False,
            }
        barrier_version = int(row.barrier_version or 0)
        quiet_since = int(row.quiet_since_version) if row.quiet_since_version is not None else None
        return {
            "exists": True,
            "barrier_version": barrier_version,
            "quiet_since_version": quiet_since,
            "last_proof_at": row.last_proof_at,
            "proof_valid": (
                quiet_since is not None and quiet_since == barrier_version and row.last_proof_at is not None
            ),
        }

    def newest_work_event(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        db: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """The latest work transition, for "the proof postdates the newest work"."""
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                select(StrategyExecutionBarrierEvent)
                .where(
                    StrategyExecutionBarrierEvent.account_id == account_id,
                    StrategyExecutionBarrierEvent.strategy_id == strategy_id,
                    StrategyExecutionBarrierEvent.execution_environment == execution_environment,
                    StrategyExecutionBarrierEvent.event.in_(WORK_EVENTS),
                )
                .order_by(
                    StrategyExecutionBarrierEvent.version.desc(),
                    StrategyExecutionBarrierEvent.created_at.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        finally:
            if owns_db:
                session.close()
        if row is None:
            return None
        return {
            "event": str(row.event),
            "version": int(row.version or 0),
            "ref": row.ref,
            "created_at": row.created_at,
        }

    # ---------------------------------------------------------------- internal

    @staticmethod
    def _load_barrier(
        session: Any, account_id: str, strategy_id: str, execution_environment: str
    ) -> Optional[Any]:
        return session.execute(
            select(StrategyExecutionBarrier).where(
                StrategyExecutionBarrier.account_id == account_id,
                StrategyExecutionBarrier.strategy_id == strategy_id,
                StrategyExecutionBarrier.execution_environment == execution_environment,
            )
        ).scalar_one_or_none()
