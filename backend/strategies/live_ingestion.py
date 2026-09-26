"""Async outcome consumer for hosted live submissions (Phase 1).

A broker acceptance is PENDING, never filled. This consumer closes the loop from
the platform's ordinary ingestion fact tables::

``live_plan_submissions.pending`` -> ``order_trade_fills`` for the bound broker
order ids -> ``partial``/``finalizing``/``filled`` -> barrier ``work_resolved``
-> attribution recompute -> reservation consumption.

Six properties carry the design, and each one is a test rather than a hope:

1. **Single writer per step.** The claim row carries a consumer lease
   (``consumer_token`` / ``consumer_until``) taken by a conditional UPDATE. Two
   consumer instances scanning the same row can both read it, but only one
   updates it; the loser leaves the claim untouched (the ``locked`` count). A
   lease abandoned by a crash expires by plain time comparison, so a step is
   never stranded and a later pass resumes the recorded cursor.
2. **Every effect is idempotent, in the right order.** Filled: publish
   attribution -> consume capacity -> record the barrier -> write the terminal
   claim. Rejected: release capacity -> record the barrier -> write the terminal
   claim. The cursor in ``detail`` records each confirmed stage, so a crash
   between a stage and its cursor write simply re-runs that stage; the publish
   is a full recompute, the reservation transition is status-guarded, and the
   barrier event is de-duplicated on ``(book, event, ref, detail.plan_id)``.
3. **No terminal claim before its effects are confirmed.** The terminal write
   happens only after re-reading the effects back from their own sources. A
   claim whose effects are unconfirmed stays ``finalizing``/``rejecting``, and
   those states are enumerated as in-flight work by the settlement barrier, so
   it can never disappear behind a quiet proof.
4. **Cross-publication generation consistency.** The cursor records the
   observation of ``strategy_projection_state`` before and after the publish; on
   resume the current publication must still be at least the generation the
   cursor published, otherwise the publish is redone. An outcome is never
   finalised against a book that moved backwards.
5. **Scoped, complete evidence.** A fill counts only when its broker order id is
   ALREADY bound to this account AND this plan's bound run, and it is counted
   per order, de-duplicated on the broker trade id. Terminal proof requires
   EVERY order id to carry an authoritative terminal status AND its per-order
   ingested fills to reach the broker's last-seen filled quantity — a CANCELLED
   order carrying a residual fill is not "nothing filled".
6. **Fail visible.** An unreadable source, a row that raises, or an effect that
   cannot be confirmed leaves the claim where it is and marks the pass
   ``degraded``. Nothing is swallowed into a clean-looking success.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from backend.app.database import SessionLocal
from backend.broker_api.orders.autoslice import autoslice_child_order_ids
from backend.strategies.live_adapter import LiveSubmissionStore
from backend.strategies.reservations import ReservationLedger
from backend.strategies.settlement import ExecutionBarrier

LIVE_ENVIRONMENT = "live"

#: Outcomes the consumer may still progress. Everything else is resolved truth.
SCANNABLE_STATES = ("pending", "partial", "finalizing", "rejecting", "uncertain")

#: States that may never be rewritten by a later pass.
TERMINAL_STATES = ("filled", "rejected", "no_op")

#: Broker order statuses that prove an order will never fill further.
REFUSED_ORDER_STATUSES = ("CANCELLED", "REJECTED", "LAPSED")

#: Every status the broker projection uses as a terminal outcome.
TERMINAL_ORDER_STATUSES = ("COMPLETE",) + REFUSED_ORDER_STATUSES

#: How long a consumer may hold a step before another instance may take over.
CONSUMER_LEASE_SECONDS = 120.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except ValueError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    if isinstance(value, Sequence):
        return list(value)
    return []


class LiveOutcomeConsumer:
    """Bounded, error-isolated polling of durable live submissions."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        barrier: Optional[ExecutionBarrier] = None,
        ledger: Optional[ReservationLedger] = None,
        submissions: Optional[LiveSubmissionStore] = None,
        attribution_publisher: Any = None,
        interval_seconds: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
        batch_size: int = 50,
        lease_seconds: float = CONSUMER_LEASE_SECONDS,
        sequence: Any = None,
        sequence_releaser: Any = None,
        lane_ledger: Any = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self.barrier = barrier or ExecutionBarrier(session_factory=self.session_factory)
        self.ledger = ledger or ReservationLedger(session_factory=self.session_factory)
        self.submissions = submissions or LiveSubmissionStore(
            session_factory=self.session_factory
        )
        #: The durable multi-step parent store (per-leg capacity settlement lives
        #: here, so one leg filling never consumes the whole parent reservation).
        if sequence is None:
            from backend.strategies.live_sequence import LivePlanSequence

            sequence = LivePlanSequence(
                session_factory=self.session_factory,
                submissions=self.submissions,
                ledger=self.ledger,
                barrier=self.barrier,
            )
        self.sequence = sequence
        #: The lane-owned consequence of a CONFIRMED fill (a roll's replacement
        #: proof, an option run's own trade), keyed from the parent's frozen step
        #: specification. ``None`` builds the production one.
        if lane_ledger is None:
            from backend.strategies.live_lane_ledger import LiveLaneLedger

            lane_ledger = LiveLaneLedger(session_factory=self.session_factory, sequence=sequence)
        self.lanes = lane_ledger
        #: The shared sequence release pass (a bound ``LivePlanExecutor`` method in
        #: production). ``None`` means this consumer does not release dependents -
        #: which is the safe default, and what a bare consumer in a test gets.
        self._sequence_releaser = sequence_releaser
        self._publisher = attribution_publisher
        self.interval_seconds = float(interval_seconds)
        self._clock = clock or _utcnow
        self.batch_size = int(batch_size)
        self.lease_seconds = float(lease_seconds)
        #: Stable identity of this consumer process; the lease is held under it.
        self.consumer_id = f"live-consumer-{uuid.uuid4().hex}"
        self._health: Dict[str, Any] = {
            "state": "idle",
            "consumer_id": self.consumer_id,
            "last_poll_at": None,
            "last_error": None,
            "resolved_total": 0,
            "partial_total": 0,
            "repair_required_total": 0,
            "pending_observed": 0,
            "locked_skipped": 0,
            "sequence_released_total": 0,
            "sequence_blocked_total": 0,
        }

    # ----------------------------------------------------------------- health

    def health(self) -> Dict[str, Any]:
        return dict(self._health)

    def _note(self, **values: Any) -> None:
        self._health.update(values)

    # -------------------------------------------------------------- ingestion

    def _pending_rows(self) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT submission_id, plan_id, step_no, step_ref, state,
                           strategy_id, account_id, broker_order_ids, delta_snapshot, detail
                    FROM public.live_plan_submissions
                    WHERE execution_environment = :environment
                      AND state = ANY(:states)
                    ORDER BY updated_at
                    LIMIT :limit
                    """
                ),
                {
                    "environment": LIVE_ENVIRONMENT,
                    "states": list(SCANNABLE_STATES),
                    "limit": self.batch_size,
                },
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "submission_id": str(row[0]),
                    "plan_id": str(row[1]),
                    "step_no": int(row[2] or 0),
                    "step_ref": str(row[3] or ""),
                    "state": str(row[4] or ""),
                    "strategy_id": str(row[5] or ""),
                    "account_id": str(row[6] or ""),
                    "broker_order_ids": [str(v) for v in _as_list(row[7])],
                    "delta": _as_dict(row[8]),
                    "detail": _as_dict(row[9]),
                }
            )
        return out

    def _bound_run(self, plan_id: str) -> Optional[str]:
        """The run the plan's envelope is bound to (authoritative linkage)."""
        with self.session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT sp.strategy_run_id
                    FROM public.strategy_plans pl
                    JOIN public.strategy_proposals sp ON sp.proposal_id = pl.proposal_id
                    JOIN public.strategy_run_bindings b ON b.strategy_run_id = sp.strategy_run_id
                    WHERE pl.plan_id = :plan_id
                      AND b.execution_environment = :environment
                    """
                ),
                {"plan_id": plan_id, "environment": LIVE_ENVIRONMENT},
            ).first()
        return str(row[0]) if row else None

    def _owned_orders(
        self, account_id: str, run_id: Optional[str], order_ids: Sequence[str]
    ) -> set[str]:
        """Order ids owned by THIS account AND this plan's bound run.

        Both ledgers are consulted. A row that names a DIFFERENT run is not this
        step's evidence even when the account matches, so an unrelated order on
        the same account can neither satisfy nor block the step.
        """
        if not order_ids or not run_id:
            return set()
        with self.session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT broker_order_id, strategy_run_id FROM public.live_order_intents
                    WHERE account_id = :account_id AND broker_order_id = ANY(:order_ids)
                    UNION ALL
                    SELECT broker_order_id, strategy_run_id FROM public.worker_live_execution_links
                    WHERE account_id = :account_id AND broker_order_id = ANY(:order_ids)
                    """
                ),
                {"account_id": account_id, "order_ids": list(order_ids)},
            ).fetchall()
        owned: set[str] = set()
        for broker_order_id, owner_run in rows:
            oid = str(broker_order_id or "")
            if not oid:
                continue
            if str(owner_run or "") != str(run_id):
                continue
            owned.add(oid)
        # Kite keeps the submitted order id as the autoslice parent and gives
        # every slice its own order id. The child event's execution link was
        # inherited from that parent, so cumulative fills can be read through
        # the same owned-order evidence path.
        for child_order_id in autoslice_child_order_ids(
            self.session_factory,
            account_id=account_id,
            run_id=str(run_id or ""),
            parent_order_ids=list(order_ids),
        ):
            owned.add(child_order_id)
        return owned

    def _fills_by_order(self, account_id: str, owned: set[str]) -> Dict[str, int]:
        """Per-order confirmed fill quantity, deduplicated on the broker trade id.

        Never a SUM across accounts: each order's fills are read for THIS
        account only and totalled per order, which is exactly what the terminal
        coverage check compares against the broker's own last-seen quantity.
        """
        if not owned:
            return {}
        with self.session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT trade_id, order_id, quantity
                    FROM public.order_trade_fills
                    WHERE account_id = :account_id AND order_id = ANY(:order_ids)
                    ORDER BY order_id, fill_timestamp, trade_id
                    """
                ),
                {"account_id": account_id, "order_ids": sorted(owned)},
            ).fetchall()
        seen: set[str] = set()
        totals: Dict[str, int] = {}
        for trade_id, order_id, quantity in rows:
            key = str(trade_id or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            oid = str(order_id or "")
            if oid not in owned:
                continue
            totals[oid] = totals.get(oid, 0) + int(quantity or 0)
        return totals

    def _terminal_statuses(
        self, account_id: str, owned: set[str], fills: Mapping[str, int]
    ) -> Optional[Dict[str, str]]:
        """Authoritative terminal status per order, or ``None`` when incomplete.

        Complete means, for EVERY order id: an ``order_state_projection`` row
        exists for this account, it is terminal with a status in the terminal
        vocabulary, and the ingested fill quantity for THAT order reaches the
        broker's last-seen filled quantity. Checking per order is the point:
        comparing a sum of COMPLETE orders against a book-wide fill total would
        let a CANCELLED order carrying a residual fill read as a zero-fill
        rejection.
        """
        if not owned:
            return None
        with self.session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT order_id, latest_status, terminal, last_seen_filled_quantity
                    FROM public.order_state_projection
                    WHERE account_id = :account_id AND order_id = ANY(:order_ids)
                    """
                ),
                {"account_id": account_id, "order_ids": sorted(owned)},
            ).fetchall()
        status_by_order: Dict[str, str] = {}
        last_seen: Dict[str, int] = {}
        terminal_flag: Dict[str, bool] = {}
        for order_id, status, terminal, seen_filled in rows:
            oid = str(order_id or "")
            status_by_order[oid] = str(status or "").upper()
            terminal_flag[oid] = bool(terminal)
            last_seen[oid] = int(seen_filled or 0)
        if set(status_by_order) != set(owned):
            return None  # missing evidence is UNKNOWN, never a refusal
        resolved: Dict[str, str] = {}
        for oid in owned:
            status = status_by_order[oid]
            if not terminal_flag[oid] or status not in TERMINAL_ORDER_STATUSES:
                return None
            if int(fills.get(oid, 0)) < int(last_seen.get(oid, 0)):
                return None  # this order's trades are not fully synced yet
            resolved[oid] = status
        return resolved

    # ------------------------------------------------------- evidence helpers

    @staticmethod
    def _evidence_digest(
        *,
        order_ids: Sequence[str],
        fills: Mapping[str, int],
        statuses: Optional[Mapping[str, str]],
    ) -> str:
        payload = {
            "orders": [str(value) for value in order_ids],
            "fills": {str(k): int(v) for k, v in sorted(fills.items())},
            "statuses": (
                {str(k): str(v) for k, v in sorted(statuses.items())}
                if statuses is not None
                else None
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _publication_generation(self, account_id: str, strategy_id: str) -> Optional[int]:
        if not (account_id and strategy_id):
            return None
        with self.session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT projection_version FROM public.strategy_projection_state
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND execution_environment = :environment
                    """
                ),
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "environment": LIVE_ENVIRONMENT,
                },
            ).first()
        return int(row[0] or 0) if row is not None else None

    async def _publish_attribution(
        self, *, account_id: str, strategy_id: str
    ) -> Optional[int]:
        """Publish the live book, returning the generation now on record.

        ``None`` means the evidence is NOT published; the caller must not treat
        the stage as done.
        """
        if not (account_id and strategy_id):
            return None
        try:
            if self._publisher is not None:
                await self._publisher(account_id=account_id, strategy_id=strategy_id)
            else:
                from backend.strategies.attribution import (
                    SqlAttributionStore,
                    StrategyAttributionService,
                )

                service = StrategyAttributionService(SqlAttributionStore(self.session_factory))
                await service.publish(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=LIVE_ENVIRONMENT,
                )
        except Exception as exc:  # noqa: BLE001 - unpublished evidence is not settled
            self._note(attribution_error=str(exc))
            return None
        return self._publication_generation(account_id, strategy_id)

    def _barrier_event_version(
        self, *, account_id: str, strategy_id: str, ref: str, plan_id: str
    ) -> Optional[int]:
        """The already-recorded ``work_resolved`` version for this plan step."""
        with self.session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT version FROM public.strategy_execution_barrier_events
                    WHERE account_id = :account_id
                      AND strategy_id = :strategy_id
                      AND execution_environment = :environment
                      AND event = 'work_resolved'
                      AND ref = :ref
                      AND detail ->> 'plan_id' = :plan_id
                    ORDER BY version
                    LIMIT 1
                    """
                ),
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "environment": LIVE_ENVIRONMENT,
                    "ref": ref,
                    "plan_id": plan_id,
                },
            ).first()
        return int(row[0]) if row is not None else None

    def _record_barrier_once(
        self,
        *,
        account_id: str,
        strategy_id: str,
        ref: str,
        plan_id: str,
        outcome: str,
        filled: int,
        ordered: int,
        step_no: int,
    ) -> Optional[int]:
        """Record the step's ``work_resolved`` EXACTLY once. ``None`` on failure.

        The effect and the lease check happen in ONE transaction: this consumer
        first takes the claim row ``FOR UPDATE`` and refuses unless it still
        holds a LIVE lease for the step. Only then does it record the barrier
        event once, under the book's advisory lock, de-duplicated on the step
        itself. A slow owner whose lease expired after a takeover therefore
        cannot append a second ``work_resolved``.
        """
        from sqlalchemy import text

        session = self.session_factory()
        try:
            now_expr = (
                "CURRENT_TIMESTAMP"
                if str(getattr(session.bind, "dialect", None) and session.bind.dialect.name) == "sqlite"
                else "NOW()"
            )
            held = session.execute(
                text(
                    f"""
                    SELECT 1 FROM public.live_plan_submissions
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND consumer_token = :token
                      AND consumer_until IS NOT NULL
                      AND consumer_until > {now_expr}
                    FOR UPDATE
                    """
                ),
                {
                    "plan_id": plan_id,
                    "step_no": int(step_no),
                    "token": self.consumer_id,
                },
            ).first()
            if held is None:
                # The lease was taken over (or expired): the effect belongs to
                # whoever owns the step now. Refusing here is the fence.
                session.rollback()
                self._note(last_error=f"barrier_lease_lost:{plan_id}")
                return None
            version, _created = self.barrier.record_work_event_once(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=LIVE_ENVIRONMENT,
                event="work_resolved",
                ref=ref,
                detail={
                    "plan_id": plan_id,
                    "outcome": outcome,
                    "filled_quantity": int(filled),
                    "ordered_quantity": int(ordered),
                    "recorded_by": self.consumer_id,
                },
                dedupe_key=plan_id,
                db=session,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - an unconfirmed effect is not success
            self._note(last_error=f"barrier:{plan_id}:{exc}")
            session.rollback()
            return None
        finally:
            session.close()
        return int(version) if version is not None else None

    def _parent_execution(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """The durable parent for this plan, or ``None`` for a pre-sequence plan.

        A single-leg parent exists too (every live plan materializes one), so this
        is not "multi-step only": it is "this plan has a durable protocol".
        """
        try:
            return self.sequence.get_execution(plan_id)
        except Exception:  # noqa: BLE001 - an unreadable parent keeps legacy behaviour
            return None

    def _consume_reservation(self, plan_id: str, *, filled: int, step_no: int = 0) -> bool:
        """Consume the capacity ONE leg actually used.

        For a plan with a durable parent this records the leg's terminal outcome
        and settles the PARENT reservation only when every leg is terminal: consuming
        the whole reservation after the first leg filled would take capacity away
        from legs that have not been submitted yet. A pre-sequence plan keeps the
        original per-plan transition.
        """
        if step_no and self._parent_execution(plan_id) is not None:
            # The lane's OWN domain record comes FIRST: a roll's replacement proof
            # and an option run's own trade are what the lane sizes its next step
            # from, so they must exist before the leg is declared terminal (and
            # before the sequence pass in this same poll could release a dependent
            # leg). A failure here raises: the caller keeps the step finalizing and
            # retries, rather than declaring a fill the lane never learned about.
            self.lanes.record_confirmed_fill(
                plan_id=plan_id, step_no=int(step_no), filled_total=int(filled)
            )
            settled = self.sequence.declare_leg_terminal(
                plan_id=plan_id,
                step_no=int(step_no),
                outcome="filled",
                filled=int(filled),
                ordered=int(filled),
            )
            return settled is not None
        reservation = self.ledger.for_plan(plan_id)
        if reservation is None:
            return True
        if str(reservation.get("status")) == "consumed":
            return True
        if str(reservation.get("status")) not in ("active", "renewed", "action_required"):
            return False
        try:
            self.ledger.consume(
                str(reservation["reservation_id"]),
                actor_id=self.consumer_id,
                detail={"plan_id": plan_id, "filled_quantity": int(filled)},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - keeps the step finalizing
            self._note(last_error=f"consume:{plan_id}:{exc}")
            return False

    def _release_reservation(self, plan_id: str, *, reason: str, step_no: int = 0) -> bool:
        if step_no and self._parent_execution(plan_id) is not None:
            settled = self.sequence.declare_leg_terminal(
                plan_id=plan_id,
                step_no=int(step_no),
                outcome="rejected",
                filled=0,
                ordered=0,
            )
            return settled is not None
        reservation = self.ledger.for_plan(plan_id)
        if reservation is None:
            return True
        if str(reservation.get("status")) == "released":
            return True
        try:
            self.ledger.release(
                str(reservation["reservation_id"]), reason=reason, actor_id=self.consumer_id
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._note(last_error=f"release:{plan_id}:{exc}")
            return False

    def _advance_reservation(self, plan_id: str) -> None:
        reservation = self.ledger.for_plan(plan_id)
        if reservation is None or str(reservation.get("status")) not in ("active",):
            return
        try:
            self.ledger.advance(str(reservation["reservation_id"]), actor_id=self.consumer_id)
        except Exception:  # noqa: BLE001 - a refused advance keeps prior evidence
            pass

    # ------------------------------------------------------------------- poll

    async def poll_once(self) -> Dict[str, int]:
        """One pass. Never raises for a single bad row."""
        counts = {
            "observed": 0,
            "filled": 0,
            "partial": 0,
            "rejected": 0,
            "repair_required": 0,
            "locked": 0,
            "unknown": 0,
            "sequence_released": 0,
            "sequence_blocked": 0,
        }
        row_errors = 0
        try:
            rows = self._pending_rows()
        except Exception as exc:  # noqa: BLE001 - unreadable source is not "nothing to do"
            self._note(
                state="degraded",
                last_error=str(exc),
                last_poll_at=self._clock().isoformat(),
            )
            return counts

        counts["observed"] = len(rows)
        for row in rows:
            try:
                await self._process_row(row, counts)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                row_errors += 1
                self._note(last_error=f"{row.get('plan_id')}: {exc}")
        # The SHARED sequence release pass runs after the outcome pass, on the
        # durable parent protocol. It places nothing it cannot justify from fresh
        # persisted evidence (see ``LivePlanExecutor.release_sequence``).
        if self._sequence_releaser is not None:
            try:
                released = await self._sequence_releaser()
                released = dict(released or {})
                counts["sequence_released"] = int(released.get("released") or 0)
                counts["sequence_blocked"] = int(released.get("blocked") or 0)
                counts["sequence_error"] = str(released.get("error_detail") or "")
                if released.get("errors"):
                    row_errors += int(released.get("errors") or 0)
            except Exception as exc:  # noqa: BLE001 - a bad pass is not a clean pass
                row_errors += 1
                self._note(last_error=f"sequence: {exc}")
        # A caught row error must not be reported as a clean pass.
        state = "degraded" if row_errors else "ok"
        self._note(
            state=state,
            last_poll_at=self._clock().isoformat(),
            row_errors=row_errors,
            pending_observed=counts["observed"],
            locked_skipped=counts["locked"],
            resolved_total=(
                self._health.get("resolved_total", 0)
                + counts["filled"]
                + counts["rejected"]
            ),
            partial_total=self._health.get("partial_total", 0) + counts["partial"],
            repair_required_total=(
                self._health.get("repair_required_total", 0) + counts["repair_required"]
            ),
            sequence_released_total=(
                self._health.get("sequence_released_total", 0)
                + counts["sequence_released"]
            ),
            sequence_blocked_total=(
                self._health.get("sequence_blocked_total", 0)
                + counts["sequence_blocked"]
            ),
        )
        return counts

    # --------------------------------------------------------------- per step

    async def _process_row(self, row: Mapping[str, Any], counts: Dict[str, int]) -> None:
        plan_id = str(row["plan_id"])
        step_no = int(row["step_no"])
        step_ref = str(row["step_ref"])
        orders = list(row["broker_order_ids"] or [])
        delta = dict(row["delta"] or {})
        stored_detail = dict(row["detail"] or {})
        state = str(row["state"] or "pending")
        ordered = abs(int(delta.get("quantity") or 0))
        strategy_id = str(row["strategy_id"] or "")
        account_id = str(row["account_id"] or "")
        if ordered == 0 or state in TERMINAL_STATES:
            return

        lease = self.submissions.acquire_lease(
            plan_id=plan_id,
            step_no=step_no,
            token=self.consumer_id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            # Another consumer owns this step right now: leave it alone.
            counts["locked"] += 1
            return
        # EVERYTHING below is taken from the row the lease just returned, never
        # from the pre-lease scan: another consumer may have advanced the state,
        # recorded an order id or written a cursor between the scan and the
        # conditional UPDATE, and acting on the stale copy would re-derive an
        # outcome the winner already decided.
        current_state = str(lease.get("state") or state)
        current_detail = dict(lease.get("detail") or {})
        current_orders = [str(value) for value in (lease.get("broker_order_ids") or orders)]
        lease_delta = dict(lease.get("delta_snapshot") or delta)
        current_ordered = abs(int(lease_delta.get("quantity") or ordered))
        current_step_ref = str(lease.get("step_ref") or step_ref)
        if current_state in TERMINAL_STATES:
            return
        try:
            await self._advance(
                plan_id=plan_id,
                step_no=step_no,
                step_ref=current_step_ref,
                account_id=account_id,
                strategy_id=strategy_id,
                orders=current_orders,
                ordered=current_ordered,
                state=current_state,
                stored_detail=current_detail,
                counts=counts,
            )
        finally:
            try:
                self.submissions.release_lease(
                    plan_id=plan_id, step_no=step_no, token=self.consumer_id
                )
            except Exception:  # noqa: BLE001 - the lease expires on its own
                pass

    async def _advance(
        self,
        *,
        plan_id: str,
        step_no: int,
        step_ref: str,
        account_id: str,
        strategy_id: str,
        orders: List[str],
        ordered: int,
        state: str,
        stored_detail: Dict[str, Any],
        counts: Dict[str, int],
    ) -> None:
        run_id = self._bound_run(plan_id)
        owned = self._owned_orders(account_id, run_id, orders)
        fills = self._fills_by_order(account_id, owned)
        filled_total = sum(int(value) for value in fills.values())
        statuses = self._terminal_statuses(account_id, owned, fills)
        digest = self._evidence_digest(
            order_ids=sorted(owned), fills=fills, statuses=statuses
        )

        # A step already mid-finalize RESUMES its stages from the durable cursor;
        # the cursor is the recovery record, not a re-derivation.
        if state in ("finalizing", "rejecting"):
            await self._resume(
                plan_id=plan_id,
                step_no=step_no,
                step_ref=step_ref,
                account_id=account_id,
                strategy_id=strategy_id,
                orders=orders,
                ordered=ordered,
                detail=stored_detail,
                digest=digest,
                counts=counts,
            )
            return

        if filled_total >= ordered:
            cursor = self._new_cursor(
                outcome="filled",
                digest=digest,
                filled=filled_total,
                ordered=ordered,
                generation_before=self._publication_generation(account_id, strategy_id),
            )
            self._stage(
                plan_id=plan_id,
                step_no=step_no,
                orders=orders,
                state="finalizing",
                cursor=cursor,
            )
            await self._resume(
                plan_id=plan_id,
                step_no=step_no,
                step_ref=step_ref,
                account_id=account_id,
                strategy_id=strategy_id,
                orders=orders,
                ordered=ordered,
                detail={"cursor": cursor},
                digest=digest,
                counts=counts,
            )
            return

        if filled_total > 0:
            if statuses is None:
                # Residual work and its capacity are both retained. A partial
                # fill that did not increase renews nothing.
                previous = int(
                    _as_dict(stored_detail.get("cursor")).get("filled_quantity")
                    or stored_detail.get("filled_quantity")
                    or 0
                )
                if filled_total > previous:
                    self._advance_reservation(plan_id)
                self.submissions.record_outcome(
                    plan_id=plan_id,
                    step_no=step_no,
                    state="partial",
                    detail={
                        "filled_quantity": filled_total,
                        "ordered_quantity": ordered,
                        "residual_quantity": ordered - filled_total,
                        "progress_at": filled_total > previous,
                        "blocking": "partial_fill_residual_work_retained",
                        "evidence": digest,
                    },
                    consumer_token=self.consumer_id,
                )
                await self._publish_attribution(
                    account_id=account_id, strategy_id=strategy_id
                )
                counts["partial"] += 1
                return
            if all(status in REFUSED_ORDER_STATUSES for status in statuses.values()):
                # Terminal cancel/reject WITH a residual: the residual is work a
                # human must repair (a replacement step or an explicit decision).
                # It is never reported as completed and never silently released.
                published = await self._publish_attribution(
                    account_id=account_id, strategy_id=strategy_id
                )
                detail: Dict[str, Any] = {
                    "filled_quantity": filled_total,
                    "ordered_quantity": ordered,
                    "residual_quantity": ordered - filled_total,
                    "blocking": "terminal_cancel_with_residual",
                    "repair_required": True,
                    "evidence": digest,
                    "terminal_statuses": dict(statuses),
                }
                if published is not None:
                    detail["published_generation"] = published
                self.submissions.record_outcome(
                    plan_id=plan_id,
                    step_no=step_no,
                    state="repair_required",
                    detail=detail,
                    consumer_token=self.consumer_id,
                )
                counts["repair_required"] += 1
                return
            # A COMPLETE order carrying a residual: its trades may still be
            # arriving. Unknown stays unknown.
            counts["unknown"] += 1
            return

        if statuses is not None and all(
            status in REFUSED_ORDER_STATUSES for status in statuses.values()
        ):
            cursor = self._new_cursor(
                outcome="rejected",
                digest=digest,
                filled=0,
                ordered=ordered,
                generation_before=self._publication_generation(account_id, strategy_id),
            )
            self._stage(
                plan_id=plan_id,
                step_no=step_no,
                orders=orders,
                state="rejecting",
                cursor=cursor,
            )
            await self._resume(
                plan_id=plan_id,
                step_no=step_no,
                step_ref=step_ref,
                account_id=account_id,
                strategy_id=strategy_id,
                orders=orders,
                ordered=ordered,
                detail={"cursor": cursor},
                digest=digest,
                counts=counts,
            )
            return

        # Unknown / not yet fully ingested: leave the claim, the work and the
        # capacity exactly where they are.
        counts["unknown"] += 1

    # ---------------------------------------------------------------- staging

    @staticmethod
    def _new_cursor(
        *,
        outcome: str,
        digest: str,
        filled: int,
        ordered: int,
        generation_before: Optional[int],
    ) -> Dict[str, Any]:
        return {
            "outcome": str(outcome),
            "evidence": str(digest),
            "filled_quantity": int(filled),
            "ordered_quantity": int(ordered),
            "generation_before": generation_before,
            "generation_after": None,
            "publish": False,
            "capacity": False,
            "barrier": False,
            "attempts": 0,
        }

    def _stage(
        self,
        *,
        plan_id: str,
        step_no: int,
        orders: Sequence[str],
        state: str,
        cursor: Mapping[str, Any],
    ) -> None:
        """Write the staged claim. Only the lease holder reaches this point."""
        self.submissions.record_outcome(
            plan_id=plan_id,
            step_no=step_no,
            state=state,
            broker_order_ids=list(orders),
            detail={"cursor": dict(cursor)},
            consumer_token=self.consumer_id,
        )

    @staticmethod
    def _cursor_of(detail: Mapping[str, Any]) -> Dict[str, Any]:
        cursor = _as_dict(detail.get("cursor"))
        if cursor:
            return cursor
        # Legacy staging (the flat pre-cursor keys) is still resumable.
        return {
            "outcome": "filled" if detail.get("publish") is not None else "rejected",
            "evidence": None,
            "filled_quantity": int(detail.get("filled_quantity") or 0),
            "ordered_quantity": int(detail.get("ordered_quantity") or 0),
            "generation_before": None,
            "generation_after": None,
            "publish": bool(detail.get("publish")),
            "capacity": bool(detail.get("capacity")),
            "barrier": bool(detail.get("barrier")),
            "attempts": 0,
        }

    async def _resume(
        self,
        *,
        plan_id: str,
        step_no: int,
        step_ref: str,
        account_id: str,
        strategy_id: str,
        orders: List[str],
        ordered: int,
        detail: Mapping[str, Any],
        digest: str,
        counts: Dict[str, int],
    ) -> None:
        """Resume a staged finalize. Every confirmed effect is re-verified."""
        cursor = self._cursor_of(detail)
        cursor["attempts"] = int(cursor.get("attempts") or 0) + 1
        # Cross-publication generation consistency: the evidence the cursor was
        # built on must still describe the claim. A changed digest (more fills,
        # a new terminal status) restarts the cursor, while the barrier's
        # step-keyed de-duplication keeps the effects single.
        if cursor.get("evidence") not in (None, digest):
            cursor = self._new_cursor(
                outcome=str(cursor.get("outcome") or "filled"),
                digest=digest,
                filled=int(cursor.get("filled_quantity") or 0),
                ordered=ordered,
                generation_before=self._publication_generation(account_id, strategy_id),
            )

        if str(cursor.get("outcome") or "filled") == "filled":
            await self._finalize_filled(
                plan_id=plan_id,
                step_no=step_no,
                step_ref=step_ref,
                account_id=account_id,
                strategy_id=strategy_id,
                orders=orders,
                ordered=ordered,
                cursor=cursor,
                digest=digest,
                counts=counts,
            )
            return
        await self._finalize_rejected(
            plan_id=plan_id,
            step_no=step_no,
            step_ref=step_ref,
            account_id=account_id,
            strategy_id=strategy_id,
            orders=orders,
            ordered=ordered,
            cursor=cursor,
            digest=digest,
            counts=counts,
        )

    async def _finalize_filled(
        self,
        *,
        plan_id: str,
        step_no: int,
        step_ref: str,
        account_id: str,
        strategy_id: str,
        orders: List[str],
        ordered: int,
        cursor: Dict[str, Any],
        digest: str,
        counts: Dict[str, int],
    ) -> None:
        filled = int(cursor.get("filled_quantity") or 0)

        # Stage 1: publish attribution. Publication is the exposure evidence the
        # capacity consumption refers to, so it comes FIRST.
        if not cursor.get("publish"):
            generation = await self._publish_attribution(
                account_id=account_id, strategy_id=strategy_id
            )
            if generation is None:
                cursor["blocking"] = "attribution_unpublished"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="finalizing", cursor=cursor,
                )
                counts["partial"] += 1
                return
            cursor["generation_after"] = generation
            cursor["publish"] = True
            cursor.pop("blocking", None)
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="finalizing", cursor=cursor,
            )
        else:
            generation = self._publication_generation(account_id, strategy_id)
            recorded = cursor.get("generation_after")
            if generation is None or (recorded is not None and generation < int(recorded)):
                # The published generation moved backwards (or disappeared): the
                # stage is not confirmed, so it is redone.
                cursor["publish"] = False
                cursor["blocking"] = "publication_generation_regressed"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="finalizing", cursor=cursor,
                )
                counts["partial"] += 1
                return

        # Stage 2: consume the capacity the fill actually used.
        if not cursor.get("capacity"):
            if not self._consume_reservation(plan_id, filled=filled, step_no=step_no):
                cursor["blocking"] = "capacity_unconsumed"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="finalizing", cursor=cursor,
                )
                counts["partial"] += 1
                return
            cursor["capacity"] = True
            cursor.pop("blocking", None)
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="finalizing", cursor=cursor,
            )

        # Stage 3: the barrier work event, exactly once per step.
        if not cursor.get("barrier"):
            version = self._record_barrier_once(
                account_id=account_id,
                strategy_id=strategy_id,
                ref=step_ref,
                plan_id=plan_id,
                outcome="filled",
                filled=filled,
                ordered=ordered,
                step_no=step_no,
            )
            if version is None:
                cursor["blocking"] = "barrier_unrecorded"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="finalizing", cursor=cursor,
                )
                counts["partial"] += 1
                return
            cursor["barrier_version"] = int(version)
            cursor["barrier"] = True
            cursor.pop("blocking", None)
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="finalizing", cursor=cursor,
            )

        # Terminal: only after every effect is re-readable from its own source.
        if (
            self._barrier_event_version(
                account_id=account_id, strategy_id=strategy_id, ref=step_ref, plan_id=plan_id
            )
            is None
        ):
            cursor["barrier"] = False
            cursor["blocking"] = "barrier_effect_unconfirmed"
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="finalizing", cursor=cursor,
            )
            counts["partial"] += 1
            return
        if self._parent_execution(plan_id) is not None:
            # A parented leg is confirmed by the PARENT's own record of the leg:
            # the parent's reservation legitimately stays held while other legs
            # are still outstanding, so "consumed" is not the right readback here.
            outcome = self.sequence.leg_outcome(plan_id=plan_id, step_no=step_no)
            if str((outcome or {}).get("outcome") or "") != "filled":
                cursor["capacity"] = False
                cursor["blocking"] = "capacity_effect_unconfirmed"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="finalizing", cursor=cursor,
                )
                counts["partial"] += 1
                return
        else:
            reservation = self.ledger.for_plan(plan_id)
            if reservation is not None and str(reservation.get("status")) != "consumed":
                cursor["capacity"] = False
                cursor["blocking"] = "capacity_effect_unconfirmed"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="finalizing", cursor=cursor,
                )
                counts["partial"] += 1
                return

        cursor["evidence"] = digest
        self._stage(
            plan_id=plan_id, step_no=step_no, orders=orders, state="filled", cursor=cursor
        )
        counts["filled"] += 1

    async def _finalize_rejected(
        self,
        *,
        plan_id: str,
        step_no: int,
        step_ref: str,
        account_id: str,
        strategy_id: str,
        orders: List[str],
        ordered: int,
        cursor: Dict[str, Any],
        digest: str,
        counts: Dict[str, int],
    ) -> None:
        # Stage 1: release the known-unfilled residual capacity.
        if not cursor.get("capacity"):
            if not self._release_reservation(
                plan_id, reason="broker_rejected", step_no=step_no
            ):
                cursor["blocking"] = "release_unconfirmed"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="rejecting", cursor=cursor,
                )
                counts["partial"] += 1
                return
            cursor["capacity"] = True
            cursor.pop("blocking", None)
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="rejecting", cursor=cursor,
            )

        # Stage 2: the barrier work event, exactly once per step.
        if not cursor.get("barrier"):
            version = self._record_barrier_once(
                account_id=account_id,
                strategy_id=strategy_id,
                ref=step_ref,
                plan_id=plan_id,
                outcome="rejected",
                filled=0,
                ordered=ordered,
                step_no=step_no,
            )
            if version is None:
                cursor["blocking"] = "barrier_unrecorded"
                self._stage(
                    plan_id=plan_id, step_no=step_no, orders=orders,
                    state="rejecting", cursor=cursor,
                )
                counts["partial"] += 1
                return
            cursor["barrier_version"] = int(version)
            cursor["barrier"] = True
            cursor.pop("blocking", None)
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="rejecting", cursor=cursor,
            )

        if (
            self._barrier_event_version(
                account_id=account_id, strategy_id=strategy_id, ref=step_ref, plan_id=plan_id
            )
            is None
        ):
            cursor["barrier"] = False
            cursor["blocking"] = "barrier_effect_unconfirmed"
            self._stage(
                plan_id=plan_id, step_no=step_no, orders=orders,
                state="rejecting", cursor=cursor,
            )
            counts["partial"] += 1
            return

        cursor["evidence"] = digest
        self._stage(
            plan_id=plan_id, step_no=step_no, orders=orders, state="rejected", cursor=cursor
        )
        counts["rejected"] += 1

    # ------------------------------------------------------------------- loop

    async def run_forever(
        self, *, health_sink: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> None:
        self._note(state="starting")
        # The cancel handler covers the WHOLE loop, including the idle sleep:
        # otherwise a shutdown that arrives between passes leaves the health
        # snapshot reading "ok" for a task that is gone.
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 - one bad pass never kills the loop
                    self._note(state="degraded", last_error=str(exc))
                if health_sink is not None:
                    try:
                        health_sink(self.health())
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            self._note(state="stopped")
            raise
