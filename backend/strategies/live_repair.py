"""Bounded operator disposition for a live step that needs repair.

A terminal broker cancel carrying a residual fill leaves the step
``repair_required``: part of the order filled, the rest never will, and the
platform refuses to invent either a fill or a rejection for it. That state is
deliberately NOT automatically resolved — it blocks a quiet proof until a human
decides, because the residual is real exposure or real unfilled intent and only
the owner knows which.

This module holds the bounded, authorised dispositions:

* ``repair_required`` (and the provably-unsent ``releasing`` window) — see
  :meth:`LiveRepairService.abandon_residual`;
* a ``withheld`` STAGED DEPENDENT BUY whose every funding leg is terminal
  without a complete fill, so the sale proceeds it waits for can never arrive —
  see :meth:`LiveRepairService.abandon_staged_dependent`. That buy is never
  auto-released, and neither case ever re-sends an uncertain order.

Both of them:

* require the operator's own authorization (the route enforces ownership);
* refuse while the plan's evaluation authority could still fill the
  residual — an operator may not abandon work that is still live;
* record the disposition in the append-only plan trail, settle the parent's
  unused capacity by the parent's own rule, and record the step's
  ``work_resolved`` barrier event exactly once, so quiescence becomes provable;
* are idempotent (a second abandon names the existing disposition) and never
  rewrite a filled quantity.

What it does NOT do: fabricate a fill, fabricate a rejection, release capacity
that a real fill consumed, or clear anything silently. ``residual_abandoned`` is
an explicit terminal state carrying the quantity, the actor and the reason.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import text

from backend.app.database import SessionLocal
from backend.strategies.live_dispatch_fence import (
    FENCE_NOT_ATTEMPTED,
    FENCE_ORDER_KNOWN,
    LiveDispatchFence,
)
from backend.strategies.reservations import ReservationLedger
from backend.strategies.settlement import ExecutionBarrier

LIVE_ENVIRONMENT = "live"

#: The step states this disposition accepts.
#:
#: ``repair_required`` is a terminal broker cancel that left a residual.
#: ``releasing`` is the OTHER recoverable window: the release pass committed the
#: claim (``withheld -> releasing``) and the process died before the handler
#: returned an order reference. That window is genuinely ambiguous - an order may
#: exist at the broker with no id to reconcile - so it is NEVER retransmitted.
#: It becomes dispositionable only once the plan's authority is PROVABLY gone and
#: only while the claim carries no order reference; before that it just keeps
#: blocking a quiet proof, which is the honest state.
REPAIRABLE_STATES = ("repair_required", "releasing")

#: The one ``releasing`` shape that may be dispositioned: the release CAS landed
#: and the send never reached the broker. "No broker order reference on the claim"
#: is NOT enough to establish that - see ``LiveDispatchFence``, which proves
#: non-submission from the platform's own durable pre-send records (and, when a
#: send WAS attempted, from an authoritative broker read). An attempted but
#: unresolved send stays in flight and is never abandoned.
RELEASING_WITHOUT_ORDER_STATES = ("releasing",)

#: Terminal states a repeated disposition reports instead of re-applying.
ALREADY_DISPOSED_STATES = ("residual_abandoned",)

DISPOSITION_ABANDONED = "residual_abandoned"

#: The detail-level disposition recorded for the OTHER bounded case: a staged
#: dependent buy whose every funding leg died without filling. The STEP state is
#: still ``residual_abandoned`` (it is terminal and unfilled); this value is what
#: the append-only trail and the owner view read to tell the two cases apart.
DISPOSITION_STAGED_DEPENDENT_ABANDONED = "staged_dependent_abandoned"


class LiveRepairRefusal(RuntimeError):
    """A named refusal from the repair disposition."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveRepairService:
    """Apply a bounded residual disposition to one live plan step."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        barrier: Optional[ExecutionBarrier] = None,
        ledger: Optional[ReservationLedger] = None,
        clock: Optional[Callable[[], datetime]] = None,
        authority_reader: Any = None,
        sequence: Any = None,
        dispatch_fence: Any = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self.barrier = barrier or ExecutionBarrier(session_factory=self.session_factory)
        self.ledger = ledger or ReservationLedger(session_factory=self.session_factory)
        self._clock = clock or _utcnow
        #: The durable parent protocol. Per-leg capacity settlement lives there, so
        #: abandoning ONE residual never releases another leg's allocation.
        if sequence is None:
            from backend.strategies.live_sequence import LivePlanSequence

            sequence = LivePlanSequence(
                session_factory=self.session_factory,
                ledger=self.ledger,
                barrier=self.barrier,
                clock=self._clock,
            )
        self.sequence = sequence
        #: ``None`` means "do not attempt to prove authority is gone": the
        #: disposition is then allowed for a step whose run binding is already
        #: closed, which is the only case the route accepts without a reader.
        self._authority_reader = authority_reader
        #: The durable PRE-SEND fence, which is what makes a ``releasing`` window
        #: decidable: it proves non-submission from platform records, and an
        #: attempted-but-unresolved send stays in flight.
        self.dispatch_fence = dispatch_fence or LiveDispatchFence(
            session_factory=self.session_factory, clock=self._clock
        )

    # ---------------------------------------------------------------- reads

    def _step(self, plan_id: str, step_no: int) -> Mapping[str, Any]:
        with self.session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT submission_id, plan_id, step_no, step_ref, state, strategy_id,
                           account_id, broker_order_ids, delta_snapshot, detail
                    FROM public.live_plan_submissions
                    WHERE plan_id = :plan_id AND step_no = :step_no
                    """
                ),
                {"plan_id": str(plan_id), "step_no": int(step_no)},
            ).mappings().first()
        if row is None:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_STEP_NOT_FOUND",
                {"plan_id": str(plan_id), "step_no": int(step_no)},
            )
        return dict(row)

    @staticmethod
    def _detail_of(step: Mapping[str, Any]) -> Dict[str, Any]:
        detail = step.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail or "{}")
            except ValueError:
                detail = {}
        return dict(detail or {})

    #: Authority refusal codes that PROVE the plan cannot place or receive new
    #: work. Anything else - a missing run, a missing hosting job, a missing
    #: token, an unreadable source, or a code this list does not name - is UNKNOWN
    #: evidence, and unknown never authorises a disposition.
    AUTHORITY_GONE_CODES = frozenset(
        {
            "RUN_NOT_OPEN",
            "TOKEN_NOT_ACTIVE",
            "TOKEN_EXPIRED",
            "HOSTED_STOP_REQUESTED",
            "HOSTED_JOB_NOT_AUTHORITY",
        }
    )

    def _authority_state(
        self, plan_id: str, *, db: Any = None
    ) -> tuple[str, Dict[str, Any]]:
        """``("live"|"gone"|"unknown", detail)`` for the plan's own authority.

        Three answers, not two, because conflating them is how a residual gets
        abandoned while it might still fill:

        * ``live``    - the plan's evaluation authority is still usable;
        * ``gone``    - the authority is PROVABLY withdrawn (the bound run left an
          authority status, the credential is revoked/expired, the operator asked
          the attempt to stop, the hosting job is no longer an authority);
        * ``unknown`` - the evidence is missing, unreadable or inconclusive (a
          missing run/job/token, an unavailable source). Unknown keeps the
          disposition REFUSED: "cannot tell" is not "cannot fill".
        """
        if self._authority_reader is not None:
            return self._reader_authority_state(plan_id)
        owns = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                text(
                    """
                    SELECT pl.plan_id AS plan_id,
                           r.status AS run_status,
                           t.status AS token_status,
                           t.expires_at AS expires_at,
                           j.desired_state AS desired_state
                    FROM public.strategy_plans pl
                    LEFT JOIN public.strategy_proposals sp ON sp.proposal_id = pl.proposal_id
                    LEFT JOIN public.algo_worker_runs r ON r.strategy_run_id = sp.strategy_run_id
                    LEFT JOIN public.algo_worker_tokens t ON t.token_id = r.token_id
                    LEFT JOIN public.strategy_jobs j ON j.run_id = r.strategy_run_id
                    WHERE pl.plan_id = :plan_id
                    ORDER BY j.created_at DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"plan_id": str(plan_id)},
            ).mappings().first()
        except Exception as exc:  # noqa: BLE001 - unreadable evidence is UNKNOWN
            if owns:
                session.close()
            return "unknown", {"plan_id": str(plan_id), "error": str(exc)}
        if owns:
            session.close()
        if row is None:
            return "unknown", {"plan_id": str(plan_id), "reason": "PLAN_NOT_FOUND"}
        run_status = row["run_status"]
        token_status = row["token_status"]
        if run_status is None or str(run_status) == "":
            # No bound run: there is nothing to prove the residual cannot fill, so
            # the honest answer is unknown rather than "nothing can fill".
            return "unknown", {"plan_id": str(plan_id), "reason": "RUN_MISSING"}
        if str(row["desired_state"] or "") == "stopped":
            return "gone", {"plan_id": str(plan_id), "reason": "HOSTED_STOP_REQUESTED"}
        if str(run_status) not in ("open", "exiting"):
            return "gone", {
                "plan_id": str(plan_id),
                "reason": "RUN_NOT_OPEN",
                "run_status": str(run_status),
            }
        if token_status is None or str(token_status) == "":
            return "unknown", {"plan_id": str(plan_id), "reason": "TOKEN_MISSING"}
        if str(token_status) != "active":
            return "gone", {
                "plan_id": str(plan_id),
                "reason": "TOKEN_NOT_ACTIVE",
                "token_status": str(token_status),
            }
        expires_at = row["expires_at"]
        if expires_at is None:
            # An unreadable expiry is incomplete evidence: it could still be live.
            return "unknown", {"plan_id": str(plan_id), "reason": "TOKEN_EXPIRY_UNREADABLE"}
        if getattr(expires_at, "tzinfo", None) is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= self._clock():
            return "gone", {
                "plan_id": str(plan_id),
                "reason": "TOKEN_EXPIRED",
                "expires_at": expires_at.isoformat(),
            }
        return "live", {"plan_id": str(plan_id), "run_status": str(run_status)}

    def _reader_authority_state(self, plan_id: str) -> tuple[str, Dict[str, Any]]:
        """Classify the platform authority READER's answer, never a bare bool.

        The reader's contract is ``(plan=..., binding=None) -> authority``: it
        needs the frozen plan to derive the binding, so this resolves the plan from
        the platform's own store rather than passing an id it cannot use.
        """
        from backend.strategies.live_authority import LiveAuthorityRefusal
        from backend.strategies.proposals import ProposalStore

        plan = ProposalStore(session_factory=self.session_factory).get_plan(str(plan_id))
        if plan is None:
            return "unknown", {"plan_id": str(plan_id), "reason": "PLAN_NOT_FOUND"}
        try:
            self._authority_reader(plan=plan)
        except LiveAuthorityRefusal as exc:
            code = str(getattr(exc, "reason_code", "") or "")
            detail = dict(getattr(exc, "detail", {}) or {})
            if code in self.AUTHORITY_GONE_CODES:
                return "gone", {"plan_id": str(plan_id), "reason": code, **detail}
            # An unnamed refusal is not proof: it is unknown evidence.
            return "unknown", {
                "plan_id": str(plan_id),
                "reason": code or "AUTHORITY_REFUSED",
                **detail,
            }
        except Exception as exc:  # noqa: BLE001 - an unreadable reader is UNKNOWN
            return "unknown", {"plan_id": str(plan_id), "error": str(exc)}
        return "live", {"plan_id": str(plan_id), "authority": "reader"}

    # --------------------------------------------------------------- apply

    def abandon_residual(
        self,
        *,
        plan_id: str,
        step_no: Optional[int] = None,
        actor: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Disposition ONE residual, atomically, with per-leg capacity accounting.

        Everything that DECIDES is durable and transactional: the book lock is
        taken first, the authority is re-read inside that transaction, the step is
        CASed ``repair_required -> residual_abandoned`` under a row lock, the
        barrier ``work_resolved`` and the append-only audit row are written in the
        SAME transaction, so a failure anywhere leaves the step exactly as it was.

        Capacity is NOT settled from here any more. Releasing the plan's whole
        reservation because one residual was abandoned would also release the
        allocation of every leg that is still pending or withheld - and would
        release capacity that a REAL fill already backs. The parent's own
        idempotent settlement rule decides instead: release only when every leg is
        terminal and nothing filled; consume when a fill exists; retain while any
        leg is still outstanding. That effect is retryable, so a failed settlement
        is a recoverable state rather than a lost decision.
        """
        plan_id = str(plan_id)
        plan_row = self._plan_book(plan_id)
        if plan_row is None:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_PLAN_NOT_FOUND", {"plan_id": plan_id, "step_no": step_no}
            )
        account_id = str(plan_row["account_id"] or "")
        strategy_id = str(plan_row["strategy_id"] or "")

        session = self.session_factory()
        try:
            # The canonical book lock: the authority read, the CAS, the barrier
            # event and the audit row are one serialized unit per book.
            self.barrier.lock_book(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=LIVE_ENVIRONMENT,
            )
            if step_no is None:
                # The operator may name the plan, not the leg: the SERVER resolves
                # the plan's own repairable step. Zero is a refusal and more than
                # one is ambiguous, so it never silently picks a leg for them.
                step_no = self._resolve_repair_step(session, plan_id=plan_id)
            step_no = int(step_no)
            step = self._step_for_update(session, plan_id=plan_id, step_no=step_no)
            state = str(step["state"] or "")
            if state in ALREADY_DISPOSED_STATES:
                session.rollback()
                detail = self._detail_of(step)
                return {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "state": state,
                    "idempotent": True,
                    "disposition": dict(detail.get("disposition_record") or {}),
                    "capacity": dict(detail.get("capacity") or {}),
                }
            if state not in REPAIRABLE_STATES:
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_NOT_REQUIRED",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": state,
                        "message": (
                            "only a step in repair_required - or a releasing step whose "
                            "send produced no order reference - may be dispositioned"
                        ),
                    },
                )
            if state in RELEASING_WITHOUT_ORDER_STATES and list(
                step.get("broker_order_ids") or []
            ):
                # An order reference exists, so this is ordinary in-flight work:
                # ingestion owns it and abandoning it here would strand a real
                # order's fills with no owner.
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_RELEASING_HAS_ORDER",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": state,
                        "broker_order_ids": [
                            str(value) for value in (step.get("broker_order_ids") or [])
                        ],
                        "message": (
                            "this step already names a broker order, so its outcome is "
                            "ingestion's to resolve and not the operator's to abandon"
                        ),
                    },
                )
            # The ``releasing`` window is the one state where the claim tells us
            # NOTHING about whether an order exists. Non-submission has to be
            # PROVEN from the platform's own durable pre-send records (and, when a
            # send was attempted, from an authoritative broker read), because
            # "no order reference was persisted" is exactly what a lost response
            # looks like. Unknown stays in flight: nothing is abandoned, no
            # capacity is released, and the barrier keeps reporting the work.
            dispatch_fence: Optional[Dict[str, Any]] = None
            if state in RELEASING_WITHOUT_ORDER_STATES:
                dispatch_fence = self.dispatch_fence.prove(
                    account_id=str(step.get("account_id") or account_id),
                    step_ref=str(step.get("step_ref") or ""),
                    plan_id=plan_id,
                    step_no=step_no,
                )
                fence_state = str(dispatch_fence.get("state") or "")
                if fence_state == FENCE_ORDER_KNOWN:
                    # The order EXISTS. Adopting the discovered reference is the
                    # honest repair: the claim returns to ordinary in-flight work
                    # and ingestion owns the outcome. It is never a retransmit and
                    # never an abandonment.
                    session.rollback()
                    return self._adopt_discovered_order(
                        plan_id=plan_id,
                        step_no=step_no,
                        account_id=str(step.get("account_id") or account_id),
                        strategy_id=str(step.get("strategy_id") or strategy_id),
                        fence=dispatch_fence,
                        actor=str(actor),
                    )
                if fence_state != FENCE_NOT_ATTEMPTED:
                    session.rollback()
                    raise LiveRepairRefusal(
                        "LIVE_REPAIR_RELEASING_OUTCOME_UNKNOWN",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            "state": state,
                            "dispatch_fence": dict(dispatch_fence),
                            "message": (
                                "this step committed to dispatch and its send outcome "
                                "cannot be disproved; it stays in flight, keeps its "
                                "capacity and is never retransmitted"
                            ),
                        },
                    )
            authority, authority_detail = self._authority_state(plan_id, db=session)
            if authority == "live":
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_AUTHORITY_STILL_ACTIVE",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "authority": authority_detail,
                        "message": (
                            "the plan's evaluation authority is still live, so a residual "
                            "might still fill; stop the attempt first"
                        ),
                    },
                )
            if authority == "unknown":
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_AUTHORITY_UNKNOWN",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "authority": authority_detail,
                        "message": (
                            "the plan's authority could not be read conclusively; unknown "
                            "evidence does not authorise abandoning a residual"
                        ),
                    },
                )

            detail = self._detail_of(step)
            residual = int(detail.get("residual_quantity") or 0)
            filled = int(detail.get("filled_quantity") or 0)
            ordered = int(detail.get("ordered_quantity") or 0)
            if state in RELEASING_WITHOUT_ORDER_STATES and not ordered:
                # The release committed to dispatch and died before returning an
                # order reference. The quantity that MAY have been sent is the
                # frozen step's own, and the outcome is honestly UNKNOWN - the
                # disposition names that rather than inventing a fill or a refusal.
                snapshot = step.get("delta_snapshot")
                if isinstance(snapshot, str):
                    try:
                        snapshot = json.loads(snapshot or "{}")
                    except ValueError:
                        snapshot = {}
                ordered = abs(int((snapshot or {}).get("quantity") or 0))
                residual = ordered
            step_ref = str(step["step_ref"] or "")
            disposition = {
                "disposition": DISPOSITION_ABANDONED,
                "prior_state": state,
                "send_outcome": (
                    "unknown_never_retransmitted"
                    if state in RELEASING_WITHOUT_ORDER_STATES
                    else "not_sent"
                ),
                "residual_quantity": residual,
                "filled_quantity": filled,
                "ordered_quantity": ordered,
                "actor_id": str(actor),
                "reason": str(reason or "") or None,
                "recorded_at": self._clock().isoformat(),
                "authority_state": authority,
                "authority_detail": authority_detail,
            }
            if dispatch_fence is not None:
                disposition["dispatch_fence"] = dict(dispatch_fence)
            updated = session.execute(
                text(
                    """
                    UPDATE public.live_plan_submissions
                    SET state = :state,
                        detail = COALESCE(detail, '{}'::jsonb) || CAST(:detail AS jsonb),
                        updated_at = NOW()
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state = :prior_state
                    """
                ),
                {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "state": DISPOSITION_ABANDONED,
                    "prior_state": state,
                    "detail": json.dumps({"disposition_record": disposition}),
                },
            )
            if int(getattr(updated, "rowcount", 0) or 0) == 0:
                # A concurrent caller moved the step between the row lock and the
                # CAS. Re-read and report honestly: never a second audit row for
                # the same decision, never a silent success for a lost race.
                session.rollback()
                current = self._step(plan_id, step_no)
                if str(current["state"] or "") in ALREADY_DISPOSED_STATES:
                    current_detail = self._detail_of(current)
                    return {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": str(current["state"]),
                        "idempotent": True,
                        "disposition": dict(
                            current_detail.get("disposition_record") or {}
                        ),
                        "capacity": dict(current_detail.get("capacity") or {}),
                    }
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_RACE_LOST",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": str(current["state"] or ""),
                    },
                )
            # The step's work is resolved by DISPOSITION, exactly once, in the same
            # transaction as the CAS and the audit row.
            version, created = self.barrier.record_work_event_once(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=LIVE_ENVIRONMENT,
                event="work_resolved",
                ref=step_ref,
                detail={
                    "plan_id": plan_id,
                    "outcome": DISPOSITION_ABANDONED,
                    "residual_quantity": residual,
                    "filled_quantity": filled,
                    "actor_id": str(actor),
                    "reason": str(reason or "") or None,
                },
                dedupe_key=plan_id,
                db=session,
            )
            disposition["barrier_version"] = version
            disposition["barrier_created"] = created
            # The append-only plan trail is where the operator's decision lives.
            self._record_trail(session, plan_id=plan_id, step_no=step_no, disposition=disposition)
            # The parent records the leg as TERMINAL but does not settle: settling
            # needs EVERY leg terminal, and that belongs to the idempotent rule.
            self.sequence.record_leg_outcome(
                plan_id=plan_id,
                step_no=step_no,
                outcome=DISPOSITION_ABANDONED,
                filled=filled,
                ordered=ordered,
                db=session,
            )
            session.commit()
        except LiveRepairRefusal:
            session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - a partial disposition must not stand
            session.rollback()
            raise LiveRepairRefusal(
                "LIVE_REPAIR_DISPOSITION_FAILED",
                {"plan_id": plan_id, "step_no": step_no, "error": str(exc)},
            ) from exc
        finally:
            session.close()

        capacity = self._settle_capacity(plan_id=plan_id, filled=filled, actor=actor)
        disposition.update(capacity)
        self._record_capacity(plan_id=plan_id, step_no=step_no, capacity=capacity)
        return {
            "plan_id": plan_id,
            "step_no": step_no,
            "state": DISPOSITION_ABANDONED,
            "idempotent": False,
            "disposition": disposition,
        }

    #: The state a staged dependent buy is dispositioned FROM.
    STAGED_DEPENDENT_PRIOR_STATE = "withheld"

    def abandon_staged_dependent(
        self,
        *,
        plan_id: str,
        step_no: Optional[int] = None,
        actor: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Disposition ONE withheld staged buy whose every funding leg died unfilled.

        A staged CNC dependent buy exists only to be funded by its reductions. Once
        EVERY one of those reductions is terminal without a complete fill -
        rejected, cancelled, or a residual an operator abandoned - the sale
        proceeds the buy waits for can never arrive. Left alone the buy would keep
        the parent and the settlement barrier in flight forever, so this is the
        bounded, operator-authorised way out:

        * it applies ONLY to a claim in ``withheld`` that names NO broker order;
        * it requires the parent's FROZEN spec to name this step as a
          ``staged_funding_gate`` leg AND every funding leg's claim to be terminal
          without a complete fill - all read from the durable rows, never asserted
          by the caller;
        * it refuses while the plan's evaluation authority could still act, and
          refuses UNKNOWN authority evidence outright (``LIVE_REPAIR_AUTHORITY_UNKNOWN``);
        * it records the funding-leg evidence in the disposition, records the
          barrier's ``work_resolved`` exactly once, and lets the parent's own
          idempotent rule settle the reservation: CONSUMED when any leg of the
          parent filled, RELEASED when none did.

        It NEVER releases the buy, sends the buy, invents a fill or a rejection for a
        funding leg, or hands back capacity a real fill already backs.
        """
        plan_id = str(plan_id)
        plan_row = self._plan_book(plan_id)
        if plan_row is None:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_PLAN_NOT_FOUND", {"plan_id": plan_id, "step_no": step_no}
            )
        account_id = str(plan_row["account_id"] or "")
        strategy_id = str(plan_row["strategy_id"] or "")

        session = self.session_factory()
        try:
            # The canonical book lock, exactly as the residual disposition takes it:
            # the authority read, the CAS, the barrier event and the audit row are
            # one serialized unit per book.
            self.barrier.lock_book(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=LIVE_ENVIRONMENT,
            )
            if step_no is None:
                step_no = self._resolve_staged_dependent_step(session, plan_id=plan_id)
            step_no = int(step_no)
            step = self._step_for_update(session, plan_id=plan_id, step_no=step_no)
            state = str(step["state"] or "")
            if state in ALREADY_DISPOSED_STATES:
                prior = dict(self._detail_of(step).get("disposition_record") or {})
                if str(prior.get("disposition") or "") != DISPOSITION_STAGED_DEPENDENT_ABANDONED:
                    session.rollback()
                    raise LiveRepairRefusal(
                        "LIVE_REPAIR_NOT_REQUIRED",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            "state": state,
                            "disposition": str(prior.get("disposition") or ""),
                            "message": (
                                "this step was already dispositioned by a different "
                                "repair case; it is not a staged dependent buy"
                            ),
                        },
                    )
                session.rollback()
                detail = self._detail_of(step)
                return {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "state": state,
                    "idempotent": True,
                    "disposition": prior,
                    "capacity": dict(detail.get("capacity") or {}),
                }
            if state != self.STAGED_DEPENDENT_PRIOR_STATE:
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_NOT_REQUIRED",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": state,
                        "message": (
                            "only a withheld staged dependent buy may be dispositioned: a "
                            "released, partially filled, uncertain or already ordered buy "
                            "is ingestion's work, never an operator's to abandon"
                        ),
                    },
                )
            order_ids = self._order_ids(step.get("broker_order_ids"))
            if order_ids:
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_STAGED_DEPENDENT_HAS_ORDER",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": state,
                        "broker_order_ids": order_ids,
                        "message": (
                            "this buy already names a broker order, so ingestion owns its "
                            "outcome; it is never abandoned and never re-sent"
                        ),
                    },
                )
            funding = self._staged_dependent_funding(
                session, plan_id=plan_id, step_no=step_no
            )
            if not funding["eligible"]:
                session.rollback()
                refusal = dict(funding["refusal"] or {})
                raise LiveRepairRefusal(
                    str(refusal.get("reason_code") or "LIVE_REPAIR_NOT_REQUIRED"),
                    dict(refusal.get("detail") or {}),
                )
            authority, authority_detail = self._authority_state(plan_id, db=session)
            if authority == "live":
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_AUTHORITY_STILL_ACTIVE",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "authority": authority_detail,
                        "message": (
                            "the plan's evaluation authority is still live, so a funding "
                            "leg or the buy itself might still be worked; stop the attempt "
                            "first"
                        ),
                    },
                )
            if authority == "unknown":
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_AUTHORITY_UNKNOWN",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "authority": authority_detail,
                        "message": (
                            "the plan's authority could not be read conclusively; unknown "
                            "evidence does not authorise abandoning a dependent buy"
                        ),
                    },
                )

            snapshot = step.get("delta_snapshot")
            if isinstance(snapshot, str):
                try:
                    snapshot = json.loads(snapshot or "{}")
                except ValueError:
                    snapshot = {}
            ordered = abs(int((snapshot or {}).get("quantity") or 0))
            disposition = {
                "disposition": DISPOSITION_STAGED_DEPENDENT_ABANDONED,
                "prior_state": state,
                "send_outcome": "not_sent",
                "buy_quantity": ordered,
                "filled_quantity": 0,
                "blocker": "STAGED_FUNDING_REDUCTION_NOT_CONFIRMED",
                "blocked_funding_steps": list(funding["funding_steps"]),
                "funding_legs": [dict(item) for item in funding["funding_legs"]],
                "actor_id": str(actor),
                "reason": str(reason or "") or None,
                "recorded_at": self._clock().isoformat(),
                "authority_state": authority,
                "authority_detail": authority_detail,
            }
            updated = session.execute(
                text(
                    """
                    UPDATE public.live_plan_submissions
                    SET state = :state,
                        detail = COALESCE(detail, '{}'::jsonb) || CAST(:detail AS jsonb),
                        updated_at = NOW()
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state = :prior_state
                    """
                ),
                {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "state": DISPOSITION_ABANDONED,
                    "prior_state": state,
                    "detail": json.dumps({"disposition_record": disposition}),
                },
            )
            if int(getattr(updated, "rowcount", 0) or 0) == 0:
                # A concurrent caller moved the step between the row lock and the
                # CAS. Re-read and report honestly: never a second audit row for the
                # same decision, never a silent success for a lost race.
                session.rollback()
                current = self._step(plan_id, step_no)
                current_detail = self._detail_of(current)
                prior = dict(current_detail.get("disposition_record") or {})
                if str(current["state"] or "") in ALREADY_DISPOSED_STATES and str(
                    prior.get("disposition") or ""
                ) == DISPOSITION_STAGED_DEPENDENT_ABANDONED:
                    return {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": str(current["state"]),
                        "idempotent": True,
                        "disposition": prior,
                        "capacity": dict(current_detail.get("capacity") or {}),
                    }
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_RACE_LOST",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "state": str(current["state"] or ""),
                    },
                )
            # The buy's work is resolved by DISPOSITION, exactly once, in the same
            # transaction as the CAS and the audit row.
            version, created = self.barrier.record_work_event_once(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=LIVE_ENVIRONMENT,
                event="work_resolved",
                ref=str(step["step_ref"] or ""),
                detail={
                    "plan_id": plan_id,
                    "outcome": DISPOSITION_STAGED_DEPENDENT_ABANDONED,
                    "buy_quantity": ordered,
                    "filled_quantity": 0,
                    "blocked_funding_steps": list(funding["funding_steps"]),
                    "actor_id": str(actor),
                    "reason": str(reason or "") or None,
                },
                dedupe_key=plan_id,
                db=session,
            )
            disposition["barrier_version"] = version
            disposition["barrier_created"] = created
            # The append-only plan trail is where the operator's decision lives.
            self._record_trail(session, plan_id=plan_id, step_no=step_no, disposition=disposition)
            # The parent records the leg as TERMINAL but does not settle: settling
            # needs EVERY leg terminal, and that belongs to the idempotent rule.
            self.sequence.record_leg_outcome(
                plan_id=plan_id,
                step_no=step_no,
                outcome=DISPOSITION_ABANDONED,
                filled=0,
                ordered=ordered,
                db=session,
            )
            session.commit()
        except LiveRepairRefusal:
            session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - a partial disposition must not stand
            session.rollback()
            raise LiveRepairRefusal(
                "LIVE_REPAIR_DISPOSITION_FAILED",
                {"plan_id": plan_id, "step_no": step_no, "error": str(exc)},
            ) from exc
        finally:
            session.close()

        # The parent's own rule decides: any real fill anywhere in the parent means
        # the reservation is no longer the owner's to reclaim, so it is CONSUMED;
        # no fill at all means the unused allocation goes back, so it is RELEASED.
        capacity = self._settle_capacity(plan_id=plan_id, filled=0, actor=actor)
        disposition.update(capacity)
        self._record_capacity(plan_id=plan_id, step_no=step_no, capacity=capacity)
        return {
            "plan_id": plan_id,
            "step_no": step_no,
            "state": DISPOSITION_ABANDONED,
            "idempotent": False,
            "disposition": disposition,
        }

    def _staged_dependent_funding(
        self, session: Any, *, plan_id: str, step_no: int
    ) -> Dict[str, Any]:
        """Read the funding evidence for one claimed step, or refuse by name.

        Everything DECIDING is read from the platform's own rows, inside the
        caller's transaction: the parent's FROZEN spec must name this step as a
        ``staged_funding_gate`` leg with at least one dependency, and every funding
        leg's claim must exist in a terminal state that did not COMPLETE a fill.
        """
        from backend.strategies.live_sequence import (
            FUNDING_LEG_TERMINAL_UNFILLED_STATES,
            RULE_STAGED_FUNDING_GATE,
        )

        def _refuse(code: str, detail: Mapping[str, Any]) -> Dict[str, Any]:
            return {
                "eligible": False,
                "refusal": {
                    "reason_code": code,
                    "detail": {"plan_id": plan_id, "step_no": int(step_no), **dict(detail)},
                },
                "funding_steps": [],
                "funding_legs": [],
            }

        parent = self.sequence.get_execution(plan_id, db=session)
        if parent is None:
            return _refuse(
                "LIVE_REPAIR_STAGED_DEPENDENT_NO_PARENT",
                {
                    "message": (
                        "this plan has no durable parent, so its funding dependencies "
                        "cannot be read; a staged dependent buy always has one"
                    )
                },
            )
        spec = next(
            (item for item in parent["step_spec"] if int(item.step_no) == int(step_no)),
            None,
        )
        if spec is None:
            return _refuse(
                "LIVE_REPAIR_STAGED_DEPENDENT_NO_SPEC",
                {"message": "the frozen parent does not carry this step"},
            )
        funding_steps = [int(value) for value in (spec.depends_on or ())]
        if str(spec.release_rule) != RULE_STAGED_FUNDING_GATE or not funding_steps:
            return _refuse(
                "LIVE_REPAIR_STAGED_DEPENDENT_NOT_GATED",
                {
                    "release_rule": str(spec.release_rule),
                    "depends_on": funding_steps,
                    "message": (
                        "only a frozen staged-funding-gate buy behind at least one "
                        "funding reduction is dispositionable here"
                    ),
                },
            )
        rows = (
            session.execute(
                text(
                    "SELECT step_no, step_ref, state, detail FROM public.live_plan_submissions "
                    "WHERE plan_id = :plan_id"
                ),
                {"plan_id": str(plan_id)},
            )
            .mappings()
            .all()
        )
        by_step = {int(row["step_no"]): dict(row) for row in rows}
        evidence: List[Dict[str, Any]] = []
        unresolved: List[int] = []
        for funding_step in funding_steps:
            row = by_step.get(int(funding_step))
            leg_detail = self._detail_of(row or {})
            state = str((row or {}).get("state") or "")
            evidence.append(
                {
                    "step_no": int(funding_step),
                    "step_ref": str((row or {}).get("step_ref") or ""),
                    "state": state,
                    "filled_quantity": int(leg_detail.get("filled_quantity") or 0),
                    "ordered_quantity": int(leg_detail.get("ordered_quantity") or 0),
                    "residual_quantity": int(leg_detail.get("residual_quantity") or 0),
                }
            )
            if row is None or state not in FUNDING_LEG_TERMINAL_UNFILLED_STATES:
                unresolved.append(int(funding_step))
        if unresolved:
            return {
                "eligible": False,
                "refusal": {
                    "reason_code": "LIVE_REPAIR_STAGED_DEPENDENT_FUNDING_UNRESOLVED",
                    "detail": {
                        "plan_id": plan_id,
                        "step_no": int(step_no),
                        "funding_legs": evidence,
                        "unresolved_funding_steps": unresolved,
                        "message": (
                            "a funding leg of this buy is still in flight or completed a "
                            "fill, so the buy is never abandoned; only a reduction that "
                            "can no longer fill leaves the buy permanently unfunded"
                        ),
                    },
                },
                "funding_steps": funding_steps,
                "funding_legs": evidence,
            }
        return {
            "eligible": True,
            "refusal": None,
            "funding_steps": funding_steps,
            "funding_legs": evidence,
        }

    def _resolve_staged_dependent_step(self, session: Any, *, plan_id: str) -> int:
        """The plan's ONE dispositionable staged dependent buy, or a named refusal.

        A plan may hold several withheld legs, so the operator's request may name
        the plan and let the server find the buy whose funding legs are all dead.
        Zero is a refusal and more than one is ambiguous, so a leg is never silently
        picked for them. A step already dispositioned by THIS case resolves too, so a
        repeat reports the SAME decision rather than "nothing needs repair".
        """
        rows = (
            session.execute(
                text(
                    "SELECT step_no, state, broker_order_ids, detail "
                    "FROM public.live_plan_submissions WHERE plan_id = :plan_id "
                    "ORDER BY step_no"
                ),
                {"plan_id": str(plan_id)},
            )
            .mappings()
            .all()
        )
        claimed = [int(row["step_no"]) for row in rows]
        eligible: List[int] = []
        for row in rows:
            candidate = int(row["step_no"])
            state = str(row["state"] or "")
            if state in ALREADY_DISPOSED_STATES:
                prior = dict(self._detail_of(row).get("disposition_record") or {})
                if str(prior.get("disposition") or "") == DISPOSITION_STAGED_DEPENDENT_ABANDONED:
                    eligible.append(candidate)
                continue
            if state != self.STAGED_DEPENDENT_PRIOR_STATE:
                continue
            if self._order_ids(row.get("broker_order_ids")):
                continue
            if self._staged_dependent_funding(
                session, plan_id=plan_id, step_no=candidate
            )["eligible"]:
                eligible.append(candidate)
        if len(eligible) == 1:
            return eligible[0]
        if len(eligible) > 1:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_STAGED_DEPENDENT_AMBIGUOUS",
                {
                    "plan_id": str(plan_id),
                    "dispositionable_steps": eligible,
                    "message": "more than one staged dependent buy is dispositionable; name the step",
                },
            )
        raise LiveRepairRefusal(
            "LIVE_REPAIR_STAGED_DEPENDENT_NOT_FOUND",
            {
                "plan_id": str(plan_id),
                "steps": claimed,
                "message": (
                    "no withheld staged dependent buy of this plan has every funding leg "
                    "terminal without a complete fill"
                ),
            },
        )

    @staticmethod
    def _order_ids(value: Any) -> List[str]:
        """The broker order references on a claim, whichever dialect stored them."""
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except ValueError:
                value = []
        return [str(item) for item in (value or [])]

    def _adopt_discovered_order(
        self,
        *,
        plan_id: str,
        step_no: int,
        account_id: str,
        strategy_id: str,
        fence: Mapping[str, Any],
        actor: str,
    ) -> Dict[str, Any]:
        """Bind a discovered broker order to the claim the crash left unbound.

        The ``releasing`` claim reached the broker and the response was lost, so
        the ONLY honest repair is to give the claim the reference that already
        exists. The step returns to ordinary in-flight work (``pending``) with the
        order ids recorded, so ingestion attributes its fills, the barrier keeps
        the work unresolved until it is terminal, and the capacity stays held. This
        places nothing and cancels nothing: it is a repair of the claim, not a
        trade.

        Idempotent: a repeat finds the order ids already recorded and reports the
        same repair rather than appending a second trail event.
        """
        order_ids = [
            str(value) for value in (fence.get("broker_order_ids") or []) if str(value)
        ]
        if not order_ids:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_ADOPT_ORDER_MISSING",
                {"plan_id": plan_id, "step_no": step_no, "dispatch_fence": dict(fence)},
            )
        session = self.session_factory()
        try:
            self.barrier.lock_book(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=LIVE_ENVIRONMENT,
            )
            step = self._step_for_update(session, plan_id=plan_id, step_no=step_no)
            existing = [str(value) for value in (step.get("broker_order_ids") or [])]
            state = str(step["state"] or "")
            if existing and state in ("pending", "partial", "finalizing", "rejecting",
                                      "repair_required"):
                session.rollback()
                return {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "state": state,
                    "idempotent": True,
                    "recovered_order_ids": existing,
                    "dispatch_fence": dict(fence),
                }
            if state not in RELEASING_WITHOUT_ORDER_STATES:
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_NOT_REQUIRED",
                    {"plan_id": plan_id, "step_no": step_no, "state": state},
                )
            detail = self._detail_of(step)
            detail["release_recovered"] = {
                "actor_id": str(actor),
                "recorded_at": self._clock().isoformat(),
                "outcome": "broker_order_recovered",
                "dispatch_fence": dict(fence),
                "broker_order_ids": order_ids,
            }
            updated = session.execute(
                text(
                    """
                    UPDATE public.live_plan_submissions
                    SET state = 'pending',
                        broker_order_ids = CAST(:orders AS jsonb),
                        detail = COALESCE(detail, '{}'::jsonb) || CAST(:detail AS jsonb),
                        updated_at = NOW()
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state = 'releasing'
                    """
                ),
                {
                    "plan_id": plan_id,
                    "step_no": int(step_no),
                    "orders": json.dumps(order_ids),
                    "detail": json.dumps({"release_recovered": detail["release_recovered"]}),
                },
            )
            if int(getattr(updated, "rowcount", 0) or 0) == 0:
                session.rollback()
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_RACE_LOST",
                    {"plan_id": plan_id, "step_no": step_no},
                )
            session.execute(
                text(
                    """
                    INSERT INTO public.strategy_plan_execution_events
                        (id, plan_id, step_no, event, actor_id, detail, created_at)
                    VALUES (:id, :plan_id, :step_no, 'release_recovered', :actor,
                            CAST(:detail AS jsonb), NOW())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "plan_id": plan_id,
                    "step_no": int(step_no),
                    "actor": str(actor),
                    "detail": json.dumps(detail["release_recovered"]),
                },
            )
            session.commit()
        except LiveRepairRefusal:
            session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - a partial repair must not stand
            session.rollback()
            raise LiveRepairRefusal(
                "LIVE_REPAIR_ADOPT_ORDER_FAILED",
                {"plan_id": plan_id, "step_no": step_no, "error": str(exc)},
            ) from exc
        finally:
            session.close()
        return {
            "plan_id": plan_id,
            "step_no": int(step_no),
            "state": "pending",
            "idempotent": False,
            "recovered_order_ids": order_ids,
            "dispatch_fence": dict(fence),
        }

    def _record_trail(
        self,
        session: Any,
        *,
        plan_id: str,
        step_no: int,
        disposition: Mapping[str, Any],
    ) -> None:
        """Append the operator's decision to the plan trail (same transaction)."""
        session.execute(
            text(
                """
                INSERT INTO public.strategy_plan_execution_events
                    (id, plan_id, step_no, event, actor_id, detail, created_at)
                VALUES (:id, :plan_id, :step_no, 'residual_abandoned', :actor,
                        CAST(:detail AS jsonb), NOW())
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "plan_id": str(plan_id),
                "step_no": int(step_no),
                "actor": str(disposition.get("actor_id") or ""),
                "detail": json.dumps(dict(disposition)),
            },
        )

    def _settle_capacity(self, *, plan_id: str, filled: int, actor: str) -> Dict[str, Any]:
        """Release ONLY proven unused capacity; retain every outstanding leg.

        The parent's own rule decides when a plan has one. A pre-sequence plan
        (no parent row) keeps the whole-plan reservation, and even there a proven
        fill means the capacity is no longer the owner's to reclaim.
        """
        parent = self.sequence.get_execution(plan_id)
        if parent is not None:
            outcome = self.sequence.settle_parent_if_complete(
                plan_id=plan_id, actor_id=str(actor)
            )
            settled = dict(outcome.get("settlement") or {})
            return {
                "capacity_state": str(outcome.get("state") or ""),
                "capacity_released": bool(outcome.get("released")),
                "capacity_consumed": bool(outcome.get("consumed")),
                "capacity_retained": bool(outcome.get("retained")),
                "outstanding_legs": list(outcome.get("outstanding_legs") or []),
                "settlement_outcome": str(settled.get("outcome") or ""),
            }
        reservation = self.ledger.for_plan(plan_id)
        if reservation is None:
            return {"capacity_state": "none", "capacity_released": False}
        status = str(reservation.get("status") or "")
        if status == "consumed":
            return {"capacity_state": "consumed", "capacity_released": False}
        if status == "released":
            return {"capacity_state": "released", "capacity_released": True}
        if int(filled) > 0:
            # A real fill BACKS part of this reservation. The ledger has no partial
            # release, so releasing would un-fund exposure that already exists.
            try:
                self.ledger.consume(
                    str(reservation["reservation_id"]),
                    actor_id=str(actor),
                    detail={"plan_id": plan_id, "reason": "residual_abandoned_with_fill"},
                )
            except Exception as exc:  # noqa: BLE001 - named, never silent
                raise LiveRepairRefusal(
                    "LIVE_REPAIR_RELEASE_REFUSED",
                    {"plan_id": plan_id, "error": str(exc), "filled_quantity": int(filled)},
                ) from exc
            return {"capacity_state": "consumed", "capacity_released": False}
        try:
            self.ledger.release(
                str(reservation["reservation_id"]),
                reason="residual_abandoned",
                actor_id=str(actor),
            )
        except Exception as exc:  # noqa: BLE001 - named, never silent
            raise LiveRepairRefusal(
                "LIVE_REPAIR_RELEASE_REFUSED",
                {
                    "plan_id": plan_id,
                    "reservation_status": status,
                    "error": str(exc),
                },
            ) from exc
        return {"capacity_state": "released", "capacity_released": True}

    def _record_capacity(
        self, *, plan_id: str, step_no: int, capacity: Mapping[str, Any]
    ) -> None:
        """Persist the capacity outcome so a retry reports the same answer."""
        try:
            with self.session_factory() as session:
                session.execute(
                    text(
                        """
                        UPDATE public.live_plan_submissions
                        SET detail = COALESCE(detail, '{}'::jsonb)
                            || CAST(:detail AS jsonb),
                            updated_at = NOW()
                        WHERE plan_id = :plan_id AND step_no = :step_no
                        """
                    ),
                    {
                        "plan_id": str(plan_id),
                        "step_no": int(step_no),
                        "detail": json.dumps({"capacity": dict(capacity)}),
                    },
                )
                session.commit()
        except Exception:  # noqa: BLE001 - the disposition is already durable
            pass

    def _plan_book(self, plan_id: str) -> Optional[Mapping[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                text(
                    "SELECT strategy_id, account_id FROM public.strategy_plans "
                    "WHERE plan_id = :plan_id"
                ),
                {"plan_id": str(plan_id)},
            ).mappings().first()
        return dict(row) if row is not None else None

    @staticmethod
    def _step_for_update(session: Any, *, plan_id: str, step_no: int) -> Mapping[str, Any]:
        row = session.execute(
            text(
                """
                SELECT submission_id, plan_id, step_no, step_ref, state, strategy_id,
                       account_id, broker_order_ids, delta_snapshot, detail
                FROM public.live_plan_submissions
                WHERE plan_id = :plan_id AND step_no = :step_no
                FOR UPDATE
                """
            ),
            {"plan_id": str(plan_id), "step_no": int(step_no)},
        ).mappings().first()
        if row is None:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_STEP_NOT_FOUND",
                {"plan_id": str(plan_id), "step_no": int(step_no)},
            )
        return dict(row)

    @staticmethod
    def _resolve_repair_step(session: Any, *, plan_id: str) -> int:
        """The plan's ONE repairable step, or a named refusal.

        A multi-step parent has one claim per leg, so the operator's request may
        name the plan and let the server find the leg that needs repair - but only
        when exactly one does. Ambiguity is refused rather than guessed at.

        A step already disposed of resolves too, so a repeat call reports the SAME
        decision (idempotent) instead of reporting that nothing needs repair.
        """
        rows = session.execute(
            text(
                "SELECT step_no, state, broker_order_ids FROM public.live_plan_submissions "
                "WHERE plan_id = :plan_id AND state = ANY(:states) "
                "ORDER BY step_no"
            ),
            {
                "plan_id": str(plan_id),
                "states": list(REPAIRABLE_STATES + ALREADY_DISPOSED_STATES),
            },
        ).fetchall()
        candidates = [int(row[0]) for row in rows]
        repairable = [
            int(row[0])
            for row in rows
            if LiveRepairService._is_repairable_row(row)
        ]
        if len(repairable) == 1:
            return repairable[0]
        if len(repairable) > 1:
            raise LiveRepairRefusal(
                "LIVE_REPAIR_NOT_REQUIRED_AMBIGUOUS",
                {
                    "plan_id": str(plan_id),
                    "repairable_steps": repairable,
                    "message": "more than one step needs repair; name the step",
                },
            )
        if len(candidates) == 1:
            # Already dispositioned: the caller gets the recorded decision.
            return candidates[0]
        raise LiveRepairRefusal(
            "LIVE_REPAIR_NOT_REQUIRED",
            {
                "plan_id": str(plan_id),
                "message": "no step of this plan is awaiting repair",
                "disposed_steps": candidates,
            },
        )

    @staticmethod
    def _is_repairable_row(row: Any) -> bool:
        """Whether one claim row is a disposition target.

        ``repair_required`` always is. A ``releasing`` row is a target only while
        it carries NO order reference - the crash-between-CAS-and-send window. A
        releasing row that names an order is ordinary in-flight work, and it is
        resolved by ingestion, not by an operator abandoning it.
        """
        state = str(row[1] or "")
        if state not in REPAIRABLE_STATES:
            return False
        if state in RELEASING_WITHOUT_ORDER_STATES:
            orders = row[2]
            if isinstance(orders, str):
                try:
                    orders = json.loads(orders or "[]")
                except ValueError:
                    orders = []
            return not list(orders or [])
        return True

    @staticmethod
    def _state_of(session: Any, plan_id: str, step_no: int) -> str:
        row = session.execute(
            text(
                "SELECT state FROM public.live_plan_submissions "
                "WHERE plan_id = :plan_id AND step_no = :step_no"
            ),
            {"plan_id": str(plan_id), "step_no": int(step_no)},
        ).first()
        return str(row[0] or "") if row is not None else ""
