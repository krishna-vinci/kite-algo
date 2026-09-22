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

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    EXECUTION_ENVIRONMENTS,
    AccountIngestState,
    Strategy,
    StrategyApproval,
    StrategyExecutionBarrier,
    StrategyExecutionBarrierEvent,
    StrategyPlan,
    StrategyPositionProjection,
    StrategyReconciliationState,
    StrategyReservation,
    StrategyRunBinding,
    StrategySettlementAssessment,
)

__all__ = [
    "AXIS_ATTRIBUTION_FLATNESS",
    "AXIS_NO_LIVE_EVALUATION_AUTHORITY",
    "AXIS_QUIESCENCE",
    "AXIS_TERMINAL_DOMAIN_STATE",
    "BARRIER_EVENT_PROOF",
    "BARRIER_EVENT_WORK_CREATED",
    "BARRIER_EVENT_WORK_RESOLVED",
    "ExecutionBarrier",
    "InflightItem",
    "ProofResult",
    "SettlementEvidenceUnavailable",
    "SettlementService",
    "enumerate_inflight_work",
    "register_domain_adapter",
    "settlement_domain_adapters",
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


def _live_submission_inflight(
    db: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
    items: List[InflightItem],
) -> None:
    """(f) a durable live plan step that is pending or UNCERTAIN.

    The internal live adapter writes its per-step claim before the network, so a
    crash after that commit leaves work the platform can still see. A pending
    order (an accepted submission awaiting fills) and an UNCERTAIN one (a
    transport/response that never resolved) are both unresolved truth; only an
    authoritative rejection or a no-op resolves the step.
    """
    with _guarded("live_plan_submissions"):
        rows = db.execute(
            text(
                """
                SELECT step_ref, state, plan_id, broker_order_ids
                FROM public.live_plan_submissions
                WHERE account_id = :account_id
                  AND strategy_id = :strategy_id
                  AND execution_environment = :execution_environment
                  AND state IN ('pending', 'uncertain')
                ORDER BY step_ref
                """
            ),
            {
                "account_id": account_id,
                "strategy_id": strategy_id,
                "execution_environment": execution_environment,
            },
        ).fetchall()
    for row in rows:
        items.append(
            InflightItem(
                f"live_submission_{str(row[1])}",
                str(row[0]),
                {"plan_id": str(row[2]), "state": str(row[1]), "broker_order_ids": row[3]},
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
    _live_submission_inflight(
        db,
        account_id=account_id,
        strategy_id=strategy_id,
        execution_environment=execution_environment,
        items=items,
    )
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
            # Work transitions serialize against proofs on the SAME book lock:
            # a bump can never land inside another transaction's proof window.
            # Correctness does not depend on this (version arithmetic invalidates
            # any proof a mid-flight bump could race), but the serialization keeps
            # the barrier's history linear per book.
            self._lock_barrier(session, account_id, strategy_id, execution_environment)
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

    def lock_book(
        self,
        db: Any,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
    ) -> None:
        """Take the book's advisory lock on the CALLER's transaction.

        Used by reconciliation so the proof check and the unblock happen under
        the same lock the work events (create_job/execution) take - a writer can
        therefore never slip between the check and the unblock.
        """
        self._lock_barrier(db, account_id, strategy_id, execution_environment)

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


# ---------------------------------------------------------------------------
# Four-axis settlement evidence (R3 §16, plan D-3/D-5)
# ---------------------------------------------------------------------------

#: Axis names. These are the platform's four settlement axes; domain adapters
#: (below) may append further axes, but every consumer can rely on these four.
AXIS_QUIESCENCE = "quiescence"
AXIS_ATTRIBUTION_FLATNESS = "attribution_scoped_flatness"
AXIS_TERMINAL_DOMAIN_STATE = "terminal_domain_state"
AXIS_NO_LIVE_EVALUATION_AUTHORITY = "no_live_evaluation_authority"

#: Rollup vocabulary (mirrors ``ck_ssa_overall``).
SETTLED = "settled"
UNSETTLED = "unsettled"
UNKNOWN = "unknown"

#: Terminal vocabulary for axis 3/4. Kept beside the queries that read them and
#: aligned with the platform's existing usage:
#: - runs close with ``closed`` (``update_run_status`` stamps ``closed_at``);
#: - hosted jobs end ``stopped``/``failed``; ``recovery_required`` is an
#:   UNRECONCILED terminal label, so it is not terminal here;
#: - reservations end ``consumed``/``released``/``expired`` (no capacity holding).
RUN_TERMINAL_STATUSES = ("closed",)
JOB_TERMINAL_STATUSES = ("stopped", "failed")
JOB_AUTHORITY_STATUSES = ("queued", "starting", "running")
RESERVATION_TERMINAL_STATUSES = ("consumed", "released", "expired")
RESERVATION_HOLDING_STATUSES = ("active", "renewed", "action_required")

#: Strategy statuses that remove evaluation authority outright.
STRATEGY_NO_AUTHORITY_STATUSES = ("archived",)

#: Freshness bound for the broker snapshot the flatness axis checks against
#: (D-3). A stale snapshot is UNKNOWABLE truth, not flat truth:
#: ``SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS``, default 60 — the platform
#: refreshes before it decides, never decides on a stale read.
DEFAULT_BROKER_SNAPSHOT_MAX_AGE_SECONDS = 60


def broker_snapshot_max_age_seconds() -> int:
    raw = os.environ.get("SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS")
    if not raw:
        return DEFAULT_BROKER_SNAPSHOT_MAX_AGE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BROKER_SNAPSHOT_MAX_AGE_SECONDS
    return value if value >= 0 else DEFAULT_BROKER_SNAPSHOT_MAX_AGE_SECONDS


#: Domain-adapter registry (D-3): later phases (option runs, rolls, ...) extend
#: settlement by appending adapters here. Each adapter receives
#: ``(account_id=..., strategy_id=..., execution_environment=..., db=...)`` and
#: returns a list of ``{"name", "state", "detail"}`` axis contributions that
#: join the rollup. EMPTY by default: this phase registers nothing, and no
#: consumer may assume an adapter exists.
settlement_domain_adapters: List[Callable[..., List[Dict[str, Any]]]] = []


def register_domain_adapter(adapter: Callable[..., List[Dict[str, Any]]]) -> None:
    """Register one domain adapter. Adapters interpret axes; they never
    duplicate the barrier or replace the four core axes."""
    settlement_domain_adapters.append(adapter)


def _axis_digest(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _make_axis(name: str, state: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    """One axis record: satisfied/failed/unknown with its evidence digest (D-3)."""
    if state not in ("satisfied", "failed", "unknown"):
        state = UNKNOWN
    return {
        "name": name,
        "satisfied": state == "satisfied",
        "state": state,
        "evidence_digest": _axis_digest({"axis": name, "state": state, "detail": detail}),
        "detail": detail,
    }


def _as_aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _rollup(axes: List[Dict[str, Any]]) -> str:
    """All satisfied ⇒ settled; any failed ⇒ unsettled; else any unknown ⇒ unknown.

    A definitive failure is reported as ``unsettled`` even when another axis is
    unknown: unknown never releases either way, but the failure is knowledge.
    """
    states = [str(axis["state"]) for axis in axes]
    if "failed" in states:
        return UNSETTLED
    if "unknown" in states:
        return UNKNOWN
    return SETTLED


def axis_quiescence(
    barrier: ExecutionBarrier,
    db: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
    proof_not_before: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Axis 1 — a valid D-1 proof covers the book right now.

    Valid means: recorded at the CURRENT barrier version (``quiet_since_version
    == barrier_version``), which by construction postdates every work event; a
    wall-clock cross-check against the newest work event and an optional
    caller floor (the run-settlement transition) are defensive unknowns. An
    absent or invalidated proof is UNKNOWN — quiescence is never "failed",
    only not-yet-proven.
    """
    try:
        state = barrier.state(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
            db=db,
        )
        newest = barrier.newest_work_event(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
            db=db,
        )
    except SQLAlchemyError:
        return _make_axis(
            AXIS_QUIESCENCE,
            UNKNOWN,
            {"reason": "barrier_unreadable"},
        )

    base_detail: Dict[str, Any] = {
        "barrier_version": state["barrier_version"],
        "quiet_since_version": state["quiet_since_version"],
    }
    if not state["proof_valid"]:
        base_detail["reason"] = "no_valid_proof"
        return _make_axis(AXIS_QUIESCENCE, UNKNOWN, base_detail)

    last_proof_at = _as_aware(state["last_proof_at"])
    if proof_not_before is not None and last_proof_at is not None and last_proof_at < _as_aware(proof_not_before):
        base_detail["reason"] = "proof_predates_required_transition"
        return _make_axis(AXIS_QUIESCENCE, UNKNOWN, base_detail)

    if newest is not None:
        base_detail["newest_work_event"] = {
            "event": newest["event"],
            "version": newest["version"],
        }
        if int(newest["version"]) > int(state["quiet_since_version"]):
            base_detail["reason"] = "proof_predates_newest_work"
            return _make_axis(AXIS_QUIESCENCE, UNKNOWN, base_detail)
        newest_at = _as_aware(newest["created_at"])
        if newest_at is not None and last_proof_at is not None and newest_at > last_proof_at:
            base_detail["reason"] = "proof_predates_newest_work"
            return _make_axis(AXIS_QUIESCENCE, UNKNOWN, base_detail)

    base_detail["last_proof_at"] = str(state["last_proof_at"])
    return _make_axis(AXIS_QUIESCENCE, "satisfied", base_detail)


def axis_attribution_flatness(
    db: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
    now: Optional[datetime] = None,
    max_age_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Axis 2 — the strategy's OWN book is flat, verified against broker truth.

    Strategy-scoped, never account-scoped (R3 invariant 3): another strategy's
    holding on the shared broker line — the account aggregate being non-zero —
    does not unsettle THIS zero book, and the account being flat never proves
    THIS book flat. On live, the broker truth read is bounded by
    ``SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS``; a stale snapshot is an
    UNKNOWABLE truth, so the axis goes unknown (refresh-before-decide) instead
    of deciding on old numbers.
    """
    moment = now or _utcnow()
    max_age = max_age_seconds if max_age_seconds is not None else broker_snapshot_max_age_seconds()
    detail: Dict[str, Any] = {"execution_environment": execution_environment}

    try:
        rows = db.execute(
            select(StrategyPositionProjection)
            .where(
                StrategyPositionProjection.account_id == account_id,
                StrategyPositionProjection.strategy_id == strategy_id,
                StrategyPositionProjection.execution_environment == execution_environment,
                StrategyPositionProjection.net_quantity != 0,
            )
            .order_by(
                StrategyPositionProjection.identity_kind,
                StrategyPositionProjection.identity_key,
                StrategyPositionProjection.product,
            )
        ).scalars().all()
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        return _make_axis(AXIS_ATTRIBUTION_FLATNESS, UNKNOWN, {"reason": "strategy_book_unreadable"})

    legs = [
        {
            "instrument_token": int(row.instrument_token),
            "exchange": str(row.exchange),
            "tradingsymbol": str(row.tradingsymbol),
            "product": str(row.product),
            "net_quantity": int(row.net_quantity),
            "identity_kind": str(row.identity_kind),
        }
        for row in rows
    ]
    open_coordinates = {(int(row.instrument_token), str(row.product)) for row in rows}
    detail["open_legs"] = len(legs)
    detail["open_coordinates"] = sorted(f"{token}:{product}" for token, product in open_coordinates)

    broker_nets: Dict[str, int] = {}
    if execution_environment == "live":
        try:
            account_rows = db.execute(
                text(
                    """
                    SELECT instrument_token, product, net_quantity, updated_at
                    FROM public.account_positions
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        except (SQLAlchemyError, SettlementEvidenceUnavailable):
            return _make_axis(AXIS_ATTRIBUTION_FLATNESS, UNKNOWN, {"reason": "account_positions_unreadable"})
        snapshot_as_of: Optional[datetime] = None
        for row in account_rows:
            broker_nets[f"{int(row[0])}:{str(row[1])}"] = int(row[2] or 0)
            updated = _as_aware(row[3])
            if updated is not None and (snapshot_as_of is None or updated > snapshot_as_of):
                snapshot_as_of = updated
        detail["broker_snapshot_as_of"] = str(snapshot_as_of) if snapshot_as_of else None
        if open_coordinates:
            # The check is non-vacuous: truth at THIS strategy's coordinates is
            # being verified, so the snapshot must be fresh enough to trust.
            stale = snapshot_as_of is None or (moment - snapshot_as_of).total_seconds() > max_age
            if stale:
                detail["reason"] = "stale_broker_snapshot"
                detail["max_age_seconds"] = max_age
                return _make_axis(AXIS_ATTRIBUTION_FLATNESS, UNKNOWN, detail)
    else:
        # Paper and dry-run books are platform-owned truth: no broker
        # round-trip exists to go stale, so no staleness gate applies.
        try:
            paper_rows = db.execute(
                text(
                    """
                    SELECT instrument_token, product, net_quantity
                    FROM public.paper_positions
                    WHERE account_scope = :account_id
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        except (SQLAlchemyError, SettlementEvidenceUnavailable):
            return _make_axis(AXIS_ATTRIBUTION_FLATNESS, UNKNOWN, {"reason": "paper_positions_unreadable"})
        for row in paper_rows:
            broker_nets[f"{int(row[0])}:{str(row[1])}"] = int(row[2] or 0)

    if legs:
        detail["legs"] = legs
        detail["broker_nets_at_coordinates"] = {
            f"{token}:{product}": broker_nets.get(f"{token}:{product}")
            for token, product in sorted(open_coordinates)
        }
        detail["reason"] = "strategy_book_open"
        return _make_axis(AXIS_ATTRIBUTION_FLATNESS, "failed", detail)

    detail["reason"] = "strategy_book_flat"
    return _make_axis(AXIS_ATTRIBUTION_FLATNESS, "satisfied", detail)


def _job_rows(db: Any, *, account_id: str, strategy_id: str, execution_environment: str) -> List[Any]:
    """Hosted jobs pinned to THIS book (strategy + account + environment)."""
    return db.execute(
        text(
            """
            SELECT id, status FROM public.strategy_jobs
            WHERE strategy_id = :strategy_id
              AND account_scope = :account_id
              AND execution_mode = :execution_environment
            ORDER BY id ASC
            """
        ),
        {
            "strategy_id": strategy_id,
            "account_id": account_id,
            "execution_environment": execution_environment,
        },
    ).fetchall()


def _bound_run_statuses(
    db: Any, *, account_id: str, strategy_id: str, execution_environment: str
) -> Dict[str, str]:
    """Run id -> status for the runs bound to this book (empty when unbound)."""
    bound = _bound_run_ids(
        db, account_id=account_id, strategy_id=strategy_id, execution_environment=execution_environment
    )
    statuses: Dict[str, str] = {}
    for run_id in sorted(bound):
        row = db.execute(
            text(
                "SELECT status FROM public.algo_worker_runs WHERE strategy_run_id = :run_id"
            ),
            {"run_id": run_id},
        ).fetchone()
        statuses[run_id] = str(row[0]) if row is not None else ""
    return statuses


def axis_terminal_domain_state(
    db: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
) -> Dict[str, Any]:
    """Axis 3 — everything the strategy could still work through is terminal.

    Runs terminal; hosted jobs terminal; plans terminal (a plan's lifecycle IS
    its reservation — one plan claims capacity once — so a plan with no
    reservation, or one whose reservation still holds capacity, is work that
    could still start); reservations not holding capacity. Any non-terminal
    state is a definitive ``failed``; any source that cannot be read is
    ``unknown`` — it is never treated as terminal.
    """
    detail: Dict[str, Any] = {}
    unknown_sources: List[str] = []
    non_terminal = {"runs": [], "jobs": [], "plans": []}

    try:
        run_statuses = _bound_run_statuses(
            db, account_id=account_id, strategy_id=strategy_id, execution_environment=execution_environment
        )
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        run_statuses = {}
        unknown_sources.append("algo_worker_runs")
    for run_id, status in sorted(run_statuses.items()):
        if status not in RUN_TERMINAL_STATUSES:
            non_terminal["runs"].append(run_id)

    try:
        job_rows = _job_rows(
            db, account_id=account_id, strategy_id=strategy_id, execution_environment=execution_environment
        )
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        job_rows = []
        unknown_sources.append("strategy_jobs")
    for row in job_rows:
        if str(row[1] or "") not in JOB_TERMINAL_STATUSES:
            non_terminal["jobs"].append(str(row[0]))

    try:
        plan_rows = db.execute(
            select(StrategyPlan).where(
                StrategyPlan.strategy_id == strategy_id,
                StrategyPlan.account_id == account_id,
            )
        ).scalars().all()
        reservation_rows = db.execute(
            select(StrategyReservation).where(
                StrategyReservation.strategy_id == strategy_id,
                StrategyReservation.account_id == account_id,
            )
        ).scalars().all()
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        plan_rows = []
        reservation_rows = []
        unknown_sources.append("plans_reservations")

    reservation_by_plan = {str(row.plan_id): str(row.status or "") for row in reservation_rows}
    for plan in plan_rows:
        plan_id = str(plan.plan_id)
        status = reservation_by_plan.get(plan_id)
        if status is None or status not in RESERVATION_TERMINAL_STATUSES:
            non_terminal["plans"].append(plan_id)

    detail["non_terminal_runs"] = sorted(non_terminal["runs"])
    detail["non_terminal_jobs"] = sorted(non_terminal["jobs"])
    detail["non_terminal_plans"] = sorted(non_terminal["plans"])

    if non_terminal["runs"] or non_terminal["jobs"] or non_terminal["plans"]:
        detail["reason"] = "non_terminal_domain_state"
        return _make_axis(AXIS_TERMINAL_DOMAIN_STATE, "failed", detail)
    if unknown_sources:
        detail["reason"] = "domain_state_unreadable"
        detail["unavailable"] = sorted(unknown_sources)
        return _make_axis(AXIS_TERMINAL_DOMAIN_STATE, UNKNOWN, detail)
    detail["reason"] = "all_terminal"
    return _make_axis(AXIS_TERMINAL_DOMAIN_STATE, "satisfied", detail)


def axis_no_live_evaluation_authority(
    db: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
) -> Dict[str, Any]:
    """Axis 4 — nothing alive may still evaluate (and so still place work).

    No ``active`` approval for the strategy; no hosted job in an
    authority-granting state; and the strategy is archived or all its runs
    closed. Unknown sources stay unknown — authority that cannot be read is
    not authority that is absent.
    """
    detail: Dict[str, Any] = {}
    unknown_sources: List[str] = []
    blockers: Dict[str, List[str]] = {"active_approvals": [], "authority_jobs": []}

    try:
        strategy_row = db.execute(
            select(Strategy).where(Strategy.id == strategy_id)
        ).scalar_one_or_none()
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        strategy_row = None
        unknown_sources.append("strategies")
    strategy_status = str(strategy_row.status) if strategy_row is not None else ""

    try:
        approval_rows = db.execute(
            select(StrategyApproval).where(
                StrategyApproval.strategy_id == strategy_id,
                StrategyApproval.status == "active",
            )
        ).scalars().all()
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        approval_rows = []
        unknown_sources.append("strategy_approvals")
    blockers["active_approvals"] = sorted(str(row.approval_id) for row in approval_rows)

    try:
        job_rows = _job_rows(
            db, account_id=account_id, strategy_id=strategy_id, execution_environment=execution_environment
        )
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        job_rows = []
        unknown_sources.append("strategy_jobs")
    blockers["authority_jobs"] = sorted(
        str(row[0]) for row in job_rows if str(row[1] or "") in JOB_AUTHORITY_STATUSES
    )

    try:
        run_statuses = _bound_run_statuses(
            db, account_id=account_id, strategy_id=strategy_id, execution_environment=execution_environment
        )
    except (SQLAlchemyError, SettlementEvidenceUnavailable):
        run_statuses = {}
        unknown_sources.append("algo_worker_runs")
    open_runs = sorted(run_id for run_id, status in run_statuses.items() if status not in RUN_TERMINAL_STATUSES)

    detail["active_approvals"] = blockers["active_approvals"]
    detail["authority_jobs"] = blockers["authority_jobs"]
    detail["strategy_status"] = strategy_status
    detail["open_runs"] = open_runs

    authority_exists = bool(blockers["active_approvals"]) or bool(blockers["authority_jobs"])
    evaluation_ended = strategy_status in STRATEGY_NO_AUTHORITY_STATUSES or not open_runs

    if authority_exists or not evaluation_ended:
        detail["reason"] = "evaluation_authority_present"
        return _make_axis(AXIS_NO_LIVE_EVALUATION_AUTHORITY, "failed", detail)
    if unknown_sources:
        detail["reason"] = "authority_state_unreadable"
        detail["unavailable"] = sorted(unknown_sources)
        return _make_axis(AXIS_NO_LIVE_EVALUATION_AUTHORITY, UNKNOWN, detail)
    detail["reason"] = "no_evaluation_authority"
    return _make_axis(AXIS_NO_LIVE_EVALUATION_AUTHORITY, "satisfied", detail)


def _adapter_axes(db: Any, *, account_id: str, strategy_id: str, execution_environment: str) -> List[Dict[str, Any]]:
    """Run registered domain adapters; a failing adapter is UNKNOWN, never empty.

    This phase registers none (plan boundary): the hook exists so later
    phases interpret the axes without touching the barrier or the rollup.
    """
    axes: List[Dict[str, Any]] = []
    for adapter in tuple(settlement_domain_adapters):
        name = f"domain:{getattr(adapter, '__name__', 'adapter')}"
        try:
            contributions = adapter(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=execution_environment,
                db=db,
            )
            for contribution in contributions or []:
                axes.append(
                    _make_axis(
                        str(contribution.get("name") or name),
                        str(contribution.get("state") or UNKNOWN),
                        dict(contribution.get("detail") or {}),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - an exploding adapter is unknown evidence
            axes.append(_make_axis(name, UNKNOWN, {"reason": "adapter_error", "error": type(exc).__name__}))
    return axes


class SettlementService:
    """Assess and persist the four-axis settlement evidence for one book.

    The assessment runs in ONE transaction under the book's barrier lock (so
    the barrier version it records cannot race a work bump on PostgreSQL) and
    is persisted as an append-only snapshot (D-5): ``barrier_version`` and
    per-axis digests travel with it so later staleness is detectable —
    ``settled`` is never a stored state the platform trusts forever.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        barrier: Optional[ExecutionBarrier] = None,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self.barrier = barrier or ExecutionBarrier(session_factory=self.session_factory)

    # ------------------------------------------------------------------ assess

    def assess(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
        proof_not_before: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Assess one book and append the snapshot. Unknown never releases."""
        session = self.session_factory()
        try:
            self.barrier._lock_barrier(session, account_id, strategy_id, execution_environment)
            barrier_state = self.barrier.state(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=execution_environment,
                db=session,
            )
            axes = [
                axis_quiescence(
                    self.barrier,
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                    proof_not_before=proof_not_before,
                ),
                axis_attribution_flatness(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                ),
                axis_terminal_domain_state(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                ),
                axis_no_live_evaluation_authority(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                ),
            ]
            axes.extend(
                _adapter_axes(
                    session,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=execution_environment,
                )
            )
            overall = _rollup(axes)
            axes_payload = {
                str(axis["name"]): {
                    "satisfied": bool(axis["satisfied"]),
                    "state": str(axis["state"]),
                    "evidence_digest": str(axis["evidence_digest"]),
                    "detail": dict(axis["detail"]),
                }
                for axis in axes
            }
            barrier_version = int(barrier_state["barrier_version"])
            row = StrategySettlementAssessment(
                id=str(uuid.uuid4()),
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=execution_environment,
                overall=overall,
                barrier_version=barrier_version,
                axes=axes_payload,
                evidence_digest=_axis_digest(
                    {
                        "account_id": account_id,
                        "strategy_id": strategy_id,
                        "execution_environment": execution_environment,
                        "overall": overall,
                        "barrier_version": barrier_version,
                        "axes": axes_payload,
                    }
                ),
                created_at=_utcnow(),
            )
            session.add(row)
            session.flush()
            session.commit()
            return self._view(row, current_barrier_version=barrier_version)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------- reads

    def latest_assessment(
        self, *, account_id: str, strategy_id: str, execution_environment: str
    ) -> Optional[Dict[str, Any]]:
        """The newest snapshot for the book, with staleness computed on read.

        ``stale`` is derived — the assessment is never rewritten — by comparing
        the snapshot's ``barrier_version`` with the barrier's CURRENT version
        (D-5): a settled snapshot after a late fill reads as stale, not valid.
        """
        session = self.session_factory()
        try:
            row = session.execute(
                select(StrategySettlementAssessment)
                .where(
                    StrategySettlementAssessment.account_id == account_id,
                    StrategySettlementAssessment.strategy_id == strategy_id,
                    StrategySettlementAssessment.execution_environment == execution_environment,
                )
                .order_by(
                    StrategySettlementAssessment.created_at.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        finally:
            session.close()
        if row is None:
            return None
        current = self.barrier.state(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
        )["barrier_version"]
        return self._view(row, current_barrier_version=current)

    # ---------------------------------------------------------------- internal

    @staticmethod
    def _view(row: Any, *, current_barrier_version: Optional[int] = None) -> Dict[str, Any]:
        barrier_version = int(row.barrier_version or 0)
        return {
            "assessment_id": str(row.id),
            "account_id": str(row.account_id),
            "strategy_id": str(row.strategy_id),
            "execution_environment": str(row.execution_environment),
            "overall": str(row.overall),
            "barrier_version": barrier_version,
            "axes": dict(row.axes or {}),
            "evidence_digest": str(row.evidence_digest),
            "created_at": row.created_at,
            "stale": (
                current_barrier_version is not None and current_barrier_version != barrier_version
            ),
        }
