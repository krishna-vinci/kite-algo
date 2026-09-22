"""Internal live plan adapter - PREPARATORY, behind the closed public gate.

This is not live enablement. There is no environment flag here and no HTTP route:
the public live plan surfaces keep refusing exactly as they do today. What this
module provides is the internal seam a later, separately authorized bundle can
turn on: one place where a frozen plan is validated against every live control
and then dispatched through the EXISTING live intent handler, with the existing
order/fill linkage carrying acceptance and fills.

It reuses rather than rebuilds:

* ``AdmissionService`` for policy, reservation validity, catalog/quote/margin
  evidence and the exposure-increasing recheck;
* ``ApprovalService.structural_validity`` for the owner approval against the
  exact immutable plan;
* ``ReservationLedger`` for the live reservation;
* ``ExecutionBarrier`` for pre-dispatch work and proof invalidation;
* a durable per-step claim (``live_plan_submissions``) that survives restart and
  serializes concurrent instances, so a step is dispatched at most once;
* ``KiteOrdersIntentHandler`` (the same handler bootstrap wires for live) as the
  dispatch boundary - a fake broker is injected there in tests, never a network.

Evidence is INJECTED from platform services. A child's payload assertion is never
evidence: the caller passes quote/margin/catalog/evaluation-authority readings
this module only validates. Unavailable or stale evidence refuses by name.

Fills are never inferred from a submission response. Broker acceptance records an
order reference and keeps the work/reservation pending; confirmed fills come from
the existing ingestion/attribution sources through ``confirmed_fills``.

The submission itself is DURABLE (``live_plan_submissions``): one unique claim per
plan step, written before the network under the book's advisory lock, so two
adapter instances - or the same instance after a restart - can never dispatch the
same step twice. An accepted order is ``pending``; a transport failure OR a
response that names no order is ``uncertain`` (recovery required, work and
reservation retained, NEVER auto-repeated); only an explicit authoritative
refusal is ``rejected`` and may resolve the known-unfilled residual work.

Sizing is derived, never asserted: the frozen leg carries the absolute TARGET, and
the step is ``target - current attributed quantity`` (direction and quantity both
from that delta), floored to the pinned lot and recorded as the delta snapshot.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from backend.app.database import SessionLocal

from .admission import AdmissionService
from .approvals import ApprovalService
from .reservations import ReservationLedger
from .settlement import ExecutionBarrier

#: The plan kinds this preparatory adapter supports. Everything else - a
#: portfolio, a futures roll half, an option structure - is a NAMED refusal
#: rather than a guess: live support is incomplete on purpose.
LIVE_SUPPORTED_PLAN_KINDS = ("single_instrument",)

#: How old an executable quote may be before it stops being executable.
QUOTE_MAX_AGE_SECONDS = 5.0

#: How far ahead the evaluation authority must still be valid.
AUTHORITY_MIN_REMAINING_SECONDS = 1.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


class LiveRefusal(Exception):
    """A named, fail-closed refusal from the live adapter."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


@dataclass
class LiveSubmission:
    """What the adapter actually knows after dispatch - never a fill."""

    plan_id: str
    state: str  # "pending" | "rejected" | "recovery_required"
    step_ref: str
    broker_order_ids: List[str] = field(default_factory=list)
    reason_code: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "state": self.state,
            "step_ref": self.step_ref,
            "broker_order_ids": list(self.broker_order_ids),
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
        }


class LiveSubmissionStore:
    """Durable per-step claim and outcome for the live adapter."""

    def __init__(self, *, session_factory: Callable[[], Any] = SessionLocal) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _dialect(session: Any) -> str:
        bind = None
        getter = getattr(session, "get_bind", None)
        if callable(getter):
            try:
                bind = getter()
            except Exception:  # noqa: BLE001 - unknown session shape
                bind = None
        return str(getattr(getattr(bind, "dialect", None), "name", None) or "postgresql")

    @staticmethod
    def _row(row: Mapping[str, Any]) -> Dict[str, Any]:
        orders = row.get("broker_order_ids")
        if isinstance(orders, str):
            orders = json.loads(orders or "[]")
        delta = row.get("delta_snapshot")
        if isinstance(delta, str):
            delta = json.loads(delta or "{}")
        detail = row.get("detail")
        if isinstance(detail, str):
            detail = json.loads(detail or "{}")
        return {
            "submission_id": str(row.get("submission_id")),
            "plan_id": str(row.get("plan_id")),
            "step_no": int(row.get("step_no") or 0),
            "step_ref": str(row.get("step_ref") or ""),
            "state": str(row.get("state") or ""),
            "broker_order_ids": list(orders or []),
            "delta_snapshot": dict(delta or {}),
            "detail": dict(detail or {}),
        }

    def get(self, *, plan_id: str, step_no: int, db: Any = None) -> Optional[Dict[str, Any]]:
        owns = db is None
        session = db or self.session_factory()
        try:
            row = (
                session.execute(
                    text(
                        "SELECT submission_id, plan_id, step_no, step_ref, state, "
                        "broker_order_ids, delta_snapshot, detail FROM public.live_plan_submissions "
                        "WHERE plan_id = :plan_id AND step_no = :step_no"
                    ),
                    {"plan_id": str(plan_id), "step_no": int(step_no)},
                )
                .mappings()
                .first()
            )
        finally:
            if owns:
                session.close()
        return None if row is None else self._row(dict(row))

    def claim(
        self,
        *,
        plan_id: str,
        step_no: int,
        step_ref: str,
        strategy_id: str,
        account_id: str,
        execution_environment: str,
        delta_snapshot: Mapping[str, Any],
        state: str = "pending",
        broker_order_ids: Sequence[str] = (),
        detail: Optional[Mapping[str, Any]] = None,
        db: Any = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Claim the step (or return the existing claim). ``created`` says which."""
        owns = db is None
        session = db or self.session_factory()
        json_cast = (
            ":{0}" if self._dialect(session) == "sqlite" else "CAST(:{0} AS jsonb)"
        )
        try:
            result = session.execute(
                text(
                    f"""
                    INSERT INTO public.live_plan_submissions (
                        submission_id, plan_id, step_no, step_ref, strategy_id,
                        account_id, execution_environment, state, broker_order_ids,
                        delta_snapshot, detail
                    ) VALUES (
                        :submission_id, :plan_id, :step_no, :step_ref, :strategy_id,
                        :account_id, :execution_environment, :state,
                        {json_cast.format('broker_order_ids')},
                        {json_cast.format('delta_snapshot')},
                        {json_cast.format('detail')}
                    )
                    ON CONFLICT (plan_id, step_no) DO NOTHING
                    """
                ),
                {
                    "submission_id": f"live_sub_{uuid.uuid4().hex}",
                    "plan_id": str(plan_id),
                    "step_no": int(step_no),
                    "step_ref": str(step_ref),
                    "strategy_id": str(strategy_id),
                    "account_id": str(account_id),
                    "execution_environment": str(execution_environment),
                    "state": str(state),
                    "broker_order_ids": json.dumps(list(broker_order_ids)),
                    "delta_snapshot": json.dumps(dict(delta_snapshot)),
                    "detail": json.dumps(dict(detail or {})),
                },
            )
            created = int(getattr(result, "rowcount", 0) or 0) > 0
            stored = self.get(plan_id=plan_id, step_no=step_no, db=session)
            if owns:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns:
                session.close()
        if stored is None:
            raise LiveRefusal("LIVE_SUBMISSION_CLAIM_MISSING", {"plan_id": str(plan_id)})
        return stored, created

    def record_outcome(
        self,
        *,
        plan_id: str,
        step_no: int,
        state: str,
        broker_order_ids: Sequence[str] = (),
        detail: Optional[Mapping[str, Any]] = None,
        db: Any = None,
    ) -> Dict[str, Any]:
        owns = db is None
        session = db or self.session_factory()
        json_cast = (
            ":{0}" if self._dialect(session) == "sqlite" else "CAST(:{0} AS jsonb)"
        )
        try:
            session.execute(
                text(
                    f"""
                    UPDATE public.live_plan_submissions
                    SET state = :state,
                        broker_order_ids = {json_cast.format('broker_order_ids')},
                        detail = {json_cast.format('detail')},
                        updated_at = {"CURRENT_TIMESTAMP" if self._dialect(session) == "sqlite" else "NOW()"}
                    WHERE plan_id = :plan_id AND step_no = :step_no
                    """
                ),
                {
                    "plan_id": str(plan_id),
                    "step_no": int(step_no),
                    "state": str(state),
                    "broker_order_ids": json.dumps(list(broker_order_ids)),
                    "detail": json.dumps(dict(detail or {})),
                },
            )
            stored = self.get(plan_id=plan_id, step_no=step_no, db=session)
            if owns:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns:
                session.close()
        return stored or {}


class LivePlanAdapter:
    """Validate every live control, then dispatch through the existing handler."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        admission: Any = None,
        approvals: Any = None,
        ledger: Any = None,
        barrier: Any = None,
        intent_handler: Any = None,
        fill_reader: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        quote_max_age_seconds: float = QUOTE_MAX_AGE_SECONDS,
        submissions: Any = None,
        position_reader: Any = None,
        authority_reader: Any = None,
    ) -> None:
        if session_factory is None:
            session_factory = SessionLocal
        self.session_factory = session_factory
        self.admission = admission or AdmissionService(session_factory=session_factory)
        self.approvals = approvals or ApprovalService(session_factory=session_factory)
        self.ledger = ledger or ReservationLedger(session_factory=session_factory)
        self.barrier = barrier or ExecutionBarrier(session_factory=session_factory)
        self.intent_handler = intent_handler
        # The authoritative confirmed-fill source (interactive ingestion /
        # attribution). Never the dispatch response.
        self.fill_reader = fill_reader
        #: The authoritative attributed-current reader (production: attribution /
        #: account truth). A caller's claim is never the size of a live order.
        self.position_reader = position_reader
        #: The platform's current evaluation-authority reading, re-checked at the
        #: moment of dispatch. Without it the authority cannot be re-verified, so
        #: the adapter refuses rather than claiming a check it did not perform.
        self.authority_reader = authority_reader
        self._clock = clock or _utcnow
        self.quote_max_age_seconds = float(quote_max_age_seconds)
        #: Durable per-step claim/outcome: the ONLY submission state.
        self.submissions = submissions or LiveSubmissionStore(session_factory=session_factory)

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _single_leg(plan: Mapping[str, Any]) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        plan_kind = str(plan.get("plan_kind") or "")
        if plan_kind not in LIVE_SUPPORTED_PLAN_KINDS:
            raise LiveRefusal(
                "LIVE_PLAN_KIND_UNSUPPORTED",
                {
                    "plan_id": plan_id,
                    "plan_kind": plan_kind,
                    "supported": list(LIVE_SUPPORTED_PLAN_KINDS),
                    "message": "live support remains incomplete: this plan kind is not carried",
                },
            )
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        if len(legs) != 1:
            raise LiveRefusal(
                "LIVE_PLAN_COMPOUND_UNSUPPORTED",
                {"plan_id": plan_id, "plan_kind": plan_kind, "leg_count": len(legs)},
            )
        return dict(legs[0])

    def _check_binding(self, plan: Mapping[str, Any], binding: Mapping[str, Any]) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        if not binding or str(binding.get("execution_environment") or "") != "live":
            raise LiveRefusal(
                "LIVE_RUN_BINDING_REQUIRED",
                {
                    "plan_id": plan_id,
                    "execution_environment": None if not binding else str(binding.get("execution_environment")),
                    "message": "a live submission requires a live-bound run",
                },
            )
        if (
            str(binding.get("strategy_id") or "") != str(plan.get("strategy_id") or "")
            or str(binding.get("account_id") or "") != str(plan.get("account_id") or "")
        ):
            raise LiveRefusal(
                "LIVE_RUN_BINDING_MISMATCH",
                {"plan_id": plan_id, "binding": dict(binding)},
            )
        return dict(binding)

    def _check_authority(
        self, plan: Mapping[str, Any], authority: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        if not authority:
            raise LiveRefusal(
                "LIVE_EVALUATION_AUTHORITY_MISSING",
                {"plan_id": plan_id},
            )
        if (
            str(authority.get("strategy_id") or "") != str(plan.get("strategy_id") or "")
            or str(authority.get("account_id") or "") != str(plan.get("account_id") or "")
            or str(authority.get("worker_run_id") or "") != str(binding.get("strategy_run_id") or "")
        ):
            raise LiveRefusal(
                "LIVE_EVALUATION_AUTHORITY_MISMATCH",
                {"plan_id": plan_id, "authority": dict(authority)},
            )
        expires_at = _as_datetime(authority.get("expires_at"))
        if expires_at is None:
            raise LiveRefusal(
                "LIVE_EVALUATION_AUTHORITY_MISSING",
                {"plan_id": plan_id, "message": "authority carries no expiry"},
            )
        if self._clock() + timedelta(seconds=AUTHORITY_MIN_REMAINING_SECONDS) >= expires_at:
            raise LiveRefusal(
                "LIVE_EVALUATION_AUTHORITY_STALE",
                {"plan_id": plan_id, "expires_at": expires_at.isoformat()},
            )
        return dict(authority)

    def _check_approval(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        approval = self.approvals.active_for_plan(plan_id)
        if approval is None:
            raise LiveRefusal("LIVE_APPROVAL_REQUIRED", {"plan_id": plan_id})
        validity = self.approvals.structural_validity(plan, approval, now=self._clock())
        if not bool(validity.get("valid")):
            raise LiveRefusal(
                "LIVE_APPROVAL_INVALID",
                {
                    "plan_id": plan_id,
                    "approval_id": str(approval.get("approval_id") or ""),
                    "mismatched_pins": validity.get("mismatched_pins"),
                    "detail": validity.get("detail"),
                },
            )
        return dict(approval)

    def _check_reservation(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        reservation = self.ledger.for_plan(plan_id)
        if reservation is None or str(reservation.get("status") or "") not in ("active", "renewed"):
            raise LiveRefusal(
                "LIVE_RESERVATION_REQUIRED",
                {
                    "plan_id": plan_id,
                    "reservation_status": None if reservation is None else str(reservation.get("status")),
                },
            )
        if str(reservation.get("execution_environment") or "") != "live":
            raise LiveRefusal(
                "LIVE_RESERVATION_MISMATCH",
                {"plan_id": plan_id, "execution_environment": str(reservation.get("execution_environment"))},
            )
        valid_until = _as_datetime(reservation.get("valid_until"))
        if valid_until is not None and self._clock() >= valid_until:
            raise LiveRefusal(
                "LIVE_RESERVATION_EXPIRED",
                {"plan_id": plan_id, "valid_until": valid_until.isoformat()},
            )
        return dict(reservation)

    def _check_quote(self, plan: Mapping[str, Any], leg: Mapping[str, Any], quote: Mapping[str, Any]) -> Dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        if not quote or quote.get("ltp") in (None, ""):
            raise LiveRefusal("LIVE_QUOTE_MISSING", {"plan_id": plan_id})
        as_of = _as_datetime(quote.get("as_of"))
        if as_of is None:
            raise LiveRefusal("LIVE_QUOTE_MISSING", {"plan_id": plan_id, "message": "quote carries no timestamp"})
        age = (self._clock() - as_of).total_seconds()
        if age > self.quote_max_age_seconds:
            raise LiveRefusal(
                "LIVE_QUOTE_STALE",
                {"plan_id": plan_id, "age_seconds": age, "as_of": as_of.isoformat()},
            )
        if str(quote.get("instrument_id") or "") not in ("", str(leg.get("instrument_id") or "")):
            raise LiveRefusal(
                "LIVE_QUOTE_MISMATCH",
                {"plan_id": plan_id, "quote_instrument_id": str(quote.get("instrument_id"))},
            )
        return dict(quote)

    def _check_admission(
        self,
        plan: Mapping[str, Any],
        *,
        margin_evidence: Optional[Mapping[str, Any]],
        catalog_state: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        verdict = self.admission.evaluate(
            plan,
            execution_environment="live",
            now=self._clock(),
            margin_evidence=margin_evidence,
            catalog_state=catalog_state,
        )
        if not bool(verdict.admitted):
            raise LiveRefusal(
                "LIVE_ADMISSION_REFUSED",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "reason_code": str(verdict.refusal_reason or ""),
                    "detail": dict(verdict.detail or {}),
                },
            )
        return {"admitted": True, "detail": dict(verdict.detail or {})}

    # -- dispatch -----------------------------------------------------------

    def _step_ref(self, plan: Mapping[str, Any], step_no: int = 1) -> str:
        return f"live-plan:{str(plan.get('plan_id') or '')}:step:{int(step_no)}"

    # -- sizing -------------------------------------------------------------

    def _pinned_units(self, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> int:
        try:
            lot = int(leg.get("lot_size"))
        except (TypeError, ValueError):
            lot = 0
        if lot <= 0:
            raise LiveRefusal(
                "LIVE_UNITS_UNPINNED",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "message": "the frozen leg carries no pinned lot to size against",
                },
            )
        return lot

    def _resolve_delta(self, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> Dict[str, Any]:
        """The step's direction and quantity, from the ATTRIBUTED current position.

        The frozen leg carries the absolute TARGET (``signed_quantity``); without
        a current reading the size is unknown, and an unknown size refuses rather
        than becoming a default BUY of the target's magnitude.
        """
        plan_id = str(plan.get("plan_id") or "")
        target_raw = leg.get("signed_quantity")
        if target_raw is None:
            raise LiveRefusal(
                "LIVE_TARGET_MISSING",
                {"plan_id": plan_id, "instrument_id": str(leg.get("instrument_id") or "")},
            )
        target = int(target_raw)
        if self.position_reader is None:
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {
                    "plan_id": plan_id,
                    "message": "no authoritative attributed-current reader is wired",
                },
            )
        try:
            current = int(
                self.position_reader(plan=dict(plan), leg=dict(leg)) or 0
            )
        except LiveRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "error": str(exc)},
            ) from exc
        lot = self._pinned_units(plan, leg)
        delta = target - current
        quantity = abs(delta)
        if lot > 1:
            quantity = (quantity // lot) * lot
        side = "BUY" if delta > 0 else "SELL"
        return {
            "target": target,
            "current": current,
            "delta": delta,
            "side": side,
            "quantity": int(quantity),
            "lot_size": lot,
        }

    # -- submit -------------------------------------------------------------

    async def submit(
        self,
        plan: Mapping[str, Any],
        *,
        actor: str,
        run_binding: Mapping[str, Any],
        evaluation_authority: Mapping[str, Any],
        quote: Mapping[str, Any],
        margin_evidence: Optional[Mapping[str, Any]] = None,
        catalog_state: Optional[Mapping[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> LiveSubmission:
        """Validate every control, claim the step durably, then dispatch ONCE."""
        step_no = 1
        plan_id = str(plan.get("plan_id") or "")
        leg = self._single_leg(plan)
        binding = self._check_binding(plan, run_binding)
        self._check_authority(plan, evaluation_authority, binding)
        self._check_approval(plan)
        self._check_reservation(plan)
        self._check_quote(plan, leg, quote)
        self._check_admission(plan, margin_evidence=margin_evidence, catalog_state=catalog_state)
        step_ref = self._step_ref(plan, step_no)

        # ONE transaction on the CANONICAL book lock (the same lock the barrier
        # work events take): claim the step, re-check the authority, and record
        # the barrier work - so a crash after this commit leaves BOTH a durable
        # claim and the work that blocks a quiet proof. Two instances (or a
        # restart) serialize here and the loser reads the winner's row.
        account_id = str(plan.get("account_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        delta: Dict[str, Any] = {}
        claim_session = self.session_factory()
        try:
            self.barrier.lock_book(
                claim_session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment="live",
            )
            # The attributed-current read that sizes the step ALSO happens under
            # the same book lock: a concurrent writer on this book cannot change
            # the position between the size we compute and the claim we commit.
            delta = self._resolve_delta(plan, leg)
            stored, created = self.submissions.claim(
                plan_id=plan_id,
                step_no=step_no,
                step_ref=step_ref,
                strategy_id=strategy_id,
                account_id=account_id,
                execution_environment="live",
                delta_snapshot=delta,
                state="pending" if delta["quantity"] else "no_op",
                detail={"actor": actor},
                db=claim_session,
            )
            if not created:
                # Already claimed (pending/uncertain/rejected/no_op): report the
                # REAL outcome, never a second dispatch.
                claim_session.rollback()
                return self._as_submission(stored)
            # Re-read the exposure-increasing authority and the evaluation
            # authority INSIDE the claim transaction: a revocation or expiry
            # between validation and dispatch must be observed here.
            self._recheck_at_dispatch(plan, evaluation_authority, binding)
            if delta["quantity"]:
                self.barrier.record_work_event(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment="live",
                    event="work_created",
                    ref=step_ref,
                    detail={"plan_id": plan_id, "delta": delta, "actor": actor},
                    db=claim_session,
                )
            claim_session.commit()
        except LiveRefusal:
            claim_session.rollback()
            raise
        except Exception:
            claim_session.rollback()
            raise
        finally:
            claim_session.close()

        if not delta["quantity"]:
            # Nothing to send: the attributed book is already at the target. The
            # claim is the record, and no work/reservation is disturbed.
            return self._as_submission(stored)

        if self.intent_handler is None:
            raise LiveRefusal("LIVE_INTENT_HANDLER_MISSING", {"plan_id": plan_id, "step_ref": step_ref})

        from backend.algo_runtime.models import OrderIntent

        payload = {
            "session_id": session_id,
            "correlation_id": step_ref,
            "idempotency_key": step_ref,
            "order": {
                "exchange": str(leg.get("broker_exchange") or leg.get("exchange") or ""),
                "tradingsymbol": str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
                "transaction_type": delta["side"],
                "product": str(leg.get("product") or ""),
                "order_type": "MARKET",
                "quantity": abs(int(delta["quantity"])),
            },
        }
        intent = OrderIntent(intent_type="place_order", payload=payload, dedupe_key=step_ref)
        try:
            result = await self.intent_handler.handle(intent, context={"plan_id": plan_id})
        except Exception as exc:  # noqa: BLE001 - transport uncertainty is not a retry
            stored = self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="uncertain",
                detail={
                    "error": str(exc),
                    "delta": delta,
                    "note": "work and reservation are retained; never auto-repeated",
                },
            )
            return self._as_submission(stored)

        order_ids = self._accepted_order_ids(result)
        if order_ids:
            stored = self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="pending",
                broker_order_ids=order_ids,
                detail={"delta": delta, "note": "accepted: fills come from ingestion"},
            )
            return self._as_submission(stored)

        if self._is_explicit_rejection(result):
            # An AUTHORITATIVE refusal may resolve the known-unfilled residual.
            self.barrier.record_work_event(
                account_id=str(plan.get("account_id") or ""),
                strategy_id=str(plan.get("strategy_id") or ""),
                execution_environment="live",
                event="work_resolved",
                ref=step_ref,
                detail={"plan_id": plan_id, "outcome": "rejected"},
            )
            stored = self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="rejected",
                detail={"delta": delta, "result": result},
            )
            return self._as_submission(stored)

        # No order id and no authoritative refusal: the outcome is UNKNOWN. Work
        # and reservation stay held, and the step is never repeated.
        stored = self.submissions.record_outcome(
            plan_id=plan_id,
            step_no=step_no,
            state="uncertain",
            detail={
                "delta": delta,
                "result": result,
                "note": "no authoritative order reference; recovery required",
            },
        )
        return self._as_submission(stored)

    def _recheck_at_dispatch(
        self,
        plan: Mapping[str, Any],
        evaluation_authority: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> None:
        """Every exposure-increasing control is re-read at the moment of dispatch.

        The evaluation authority is re-read from the PLATFORM reader rather than
        re-trusted from the caller's copy, so an authority that expired (or was
        revoked) between validation and dispatch refuses. With no reader wired the
        adapter cannot verify it and says so instead of implying a check.
        """
        self._check_approval(plan)
        self._check_reservation(plan)
        if self.authority_reader is None:
            raise LiveRefusal(
                "LIVE_AUTHORITY_EVIDENCE_UNAVAILABLE",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "message": (
                        "no platform evaluation-authority reader is wired, so the "
                        "authority cannot be re-verified at dispatch"
                    ),
                },
            )
        try:
            current = self.authority_reader(plan=dict(plan), binding=dict(binding))
        except LiveRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_AUTHORITY_EVIDENCE_UNAVAILABLE",
                {"plan_id": str(plan.get("plan_id") or ""), "error": str(exc)},
            ) from exc
        if not current:
            raise LiveRefusal(
                "LIVE_EVALUATION_AUTHORITY_STALE",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "message": "no current evaluation authority for this run",
                },
            )
        self._check_authority(plan, current, binding)
        _ = evaluation_authority

    @staticmethod
    def _as_submission(stored: Mapping[str, Any]) -> LiveSubmission:
        state = str(stored.get("state") or "")
        return LiveSubmission(
            plan_id=str(stored.get("plan_id") or ""),
            state=state,
            step_ref=str(stored.get("step_ref") or ""),
            broker_order_ids=list(stored.get("broker_order_ids") or []),
            reason_code={
                "uncertain": "LIVE_TRANSPORT_UNCERTAIN",
                "rejected": "LIVE_ORDER_REJECTED",
            }.get(state),
            detail={
                **dict(stored.get("delta_snapshot") or {}),
                **dict(stored.get("detail") or {}),
            },
        )

    @staticmethod
    def _is_explicit_rejection(result: Any) -> bool:
        """ONLY an authoritative refusal counts as a rejection."""
        payload = dict(result or {}) if isinstance(result, Mapping) else {}
        body = payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
        if not isinstance(body, Mapping):
            return False
        status = str(body.get("status") or "").lower()
        return status in ("rejected", "refused")

    @staticmethod
    def _accepted_order_ids(result: Any) -> List[str]:
        payload = dict(result or {}) if isinstance(result, Mapping) else {}
        body = payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
        ids: List[str] = []
        for key in ("order_id", "broker_order_id"):
            value = body.get(key) if isinstance(body, Mapping) else None
            if value:
                ids.append(str(value))
        for row in (body.get("orders") if isinstance(body, Mapping) else None) or []:
            if isinstance(row, Mapping):
                value = row.get("order_id") or row.get("broker_order_id")
                if value:
                    ids.append(str(value))
        return list(dict.fromkeys(ids))

    # -- fills --------------------------------------------------------------

    def confirmed_fills(self, *, plan: Mapping[str, Any], broker_order_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Confirmed fills from the authoritative ingestion source.

        Never the dispatch response: an accepted order is pending until the
        existing ingestion reports the execution. Partial fills are returned as
        partial - residual work and the reservation stay held.
        """
        if self.fill_reader is None:
            raise LiveRefusal(
                "LIVE_FILL_SOURCE_MISSING",
                {"plan_id": str(plan.get("plan_id") or "")},
            )
        rows = self.fill_reader(broker_order_ids=list(broker_order_ids), plan=dict(plan))
        return [dict(row) for row in (rows or [])]
