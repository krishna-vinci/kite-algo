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
from backend.broker_api.orders.autoslice import should_autoslice
from backend.broker_api.orders.autoslice import (
    autoslice_child_order_ids,
    bound_run_for_plan,
    merge_order_ids,
)

from .admission import AdmissionService, margin_max_age_seconds
from .approvals import ApprovalService
from backend.options.market.freshness import (
    LIVE_OPTION_CHAIN_MAX_AGE_SECONDS,
    validate_option_chain_evidence,
)
from .live_limit_orders import (
    # Re-exported under its historical name: C1.1's drift bound and the
    # bounded-LIMIT derivation it feeds now both live in ``live_limit_orders``.
    DEFAULT_STAGED_BUY_MAX_PRICE_DRIFT_PCT,
    LIVE_LIMIT_TICK_UNKNOWN,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    LimitOrderRefusal,
    derive_bounded_limit,
    frozen_reference_price,
    gated_limit_timeout_seconds,
    is_gated_limit_step,
    option_limit_max_drift_pct,
    quote_reference_ltp,
    staged_buy_max_price_drift_pct,
)
from .live_readers import live_catalog_tick_size
from .reservations import CapacityExceeded, ReservationLedger
from .settlement import ExecutionBarrier

#: The plan kinds this adapter dispatches. Everything else - an ``intent_bundle``
#: - is a NAMED refusal rather than a guess: live support is explicit, and each
#: lane is wired through ``live_sequence.register_live_lane``.
LIVE_SUPPORTED_PLAN_KINDS = (
    "single_instrument",
    "target_weights",
    "target_futures",
    "option_structure",
)

#: How old an executable quote may be before it stops being executable.
QUOTE_MAX_AGE_SECONDS = 5.0

#: How far ahead the evaluation authority must still be valid.
AUTHORITY_MIN_REMAINING_SECONDS = 1.0

#: The ONE approval pin a released dependent leg may explain away, and only when
#: it can PROVE the book moved by nothing but this parent's own confirmed fills.
#:
#: The approval pins the exposure snapshot the owner approved against. For a
#: multi-step parent the book moves by design between legs: the reducing leg
#: fills, the attribution is republished, and the snapshot version/hash changes.
#: That is the plan working - but the SAME change is produced by another plan of
#: the run, a manual trade or a corporate action, and then the frozen delta would
#: over-target a book it no longer describes. So the pin is tolerated ONLY against
#: the per-instrument identity proof below; every other pin (plan hash,
#: reconciliation version, catalog state, session products, reservation activity,
#: approval window) is always enforced.
SEQUENCE_TOLERATED_PIN_MISMATCHES = ("EXPOSURE_SNAPSHOT_CHANGED",)


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
    #: Per-step view of a multi-step parent plan. For a one-leg plan this is the
    #: single step; the top-level fields mirror its first non-withheld step so the
    #: Phase 1 callers keep the same shape.
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "state": self.state,
            "step_ref": self.step_ref,
            "broker_order_ids": list(self.broker_order_ids),
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
            "steps": [dict(step) for step in self.steps],
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
            "consumer_token": row.get("consumer_token"),
            "consumer_until": row.get("consumer_until"),
        }

    _SELECT_COLUMNS = (
        "submission_id, plan_id, step_no, step_ref, state, broker_order_ids, "
        "delta_snapshot, detail, consumer_token, consumer_until"
    )

    def get(self, *, plan_id: str, step_no: int, db: Any = None) -> Optional[Dict[str, Any]]:
        owns = db is None
        session = db or self.session_factory()
        try:
            row = (
                session.execute(
                    text(
                        f"SELECT {self._SELECT_COLUMNS} FROM public.live_plan_submissions "
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

    def working_limit_steps(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Non-terminal GATED LIMIT claims, oldest first.

        Only steps that actually reached the broker as a bounded LIMIT (the
        durable ``execution_order`` evidence says so) and are still working
        (``pending``/``partial``) are returned. A ``releasing`` claim is NOT one
        of these: the send's outcome is unknown there, which is the pre-send
        fence's business, not the timeout's.
        """
        session = self.session_factory()
        try:
            dialect = self._dialect(session)
            if dialect == "sqlite":
                limit_filter = (
                    "json_extract(detail, '$.execution_order.order_type') = :order_type"
                )
            else:
                limit_filter = "detail -> 'execution_order' ->> 'order_type' = :order_type"
            rows = (
                session.execute(
                    text(
                        f"""
                        SELECT submission_id, plan_id, step_no, step_ref, state,
                               broker_order_ids, delta_snapshot, detail,
                               consumer_token, consumer_until
                        FROM public.live_plan_submissions
                        WHERE execution_environment = 'live'
                          AND state IN ('pending', 'partial')
                          AND {limit_filter}
                        ORDER BY updated_at
                        LIMIT :limit
                        """
                    ),
                    {"order_type": ORDER_TYPE_LIMIT, "limit": int(limit)},
                )
                .mappings()
                .all()
            )
        finally:
            session.close()
        return [self._row(dict(row)) for row in rows]

    # ------------------------------------------------------- consumer leasing

    def acquire_lease(
        self,
        *,
        plan_id: str,
        step_no: int,
        token: str,
        lease_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        """CAS the single-writer lease for one step, or ``None`` when it is held.

        Two consumer instances (or the same one after a restart) can scan the
        same row at the same moment. The conditional UPDATE is evaluated under
        the row lock, so exactly ONE caller becomes the writer: the loser updates
        zero rows and must leave the claim alone. A lease abandoned by a crash
        expires by plain comparison against ``until``, so work is never
        stranded.

        The expiry is computed from the DATABASE clock (``NOW()``), not from the
        caller's: the lease is then comparable to the same clock that later
        decides whether a write is still authorised, so an app/DB clock skew can
        neither invalidate a healthy lease nor extend an abandoned one.
        """
        owns = True
        session = self.session_factory()
        dialect = self._dialect(session)
        now_expr = "CURRENT_TIMESTAMP" if dialect == "sqlite" else "NOW()"
        if dialect == "sqlite":
            until_expr = "datetime('now', '+' || :lease_seconds || ' seconds')"
        else:
            until_expr = "NOW() + make_interval(secs => :lease_seconds)"
        try:
            row = (
                session.execute(
                    text(
                        """
                        UPDATE public.live_plan_submissions
                        SET consumer_token = :token,
                            consumer_until = {until_expr},
                            updated_at = {now}
                        WHERE plan_id = :plan_id
                          AND step_no = :step_no
                          AND state NOT IN ('filled', 'rejected', 'no_op')
                          AND (
                                consumer_token IS NULL
                             OR consumer_token = :token
                             OR consumer_until IS NULL
                             OR consumer_until <= {now}
                          )
                        RETURNING {columns}
                        """.format(
                            now=now_expr,
                            until_expr=until_expr,
                            columns=self._SELECT_COLUMNS,
                        )
                    ),
                    {
                        "plan_id": str(plan_id),
                        "step_no": int(step_no),
                        "token": str(token),
                        "lease_seconds": float(lease_seconds),
                    },
                )
                .mappings()
                .first()
            )
            if owns:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return None if row is None else self._row(dict(row))

    def release_lease(self, *, plan_id: str, step_no: int, token: str) -> None:
        """Give the lease back so a later pass (another instance) can resume."""
        session = self.session_factory()
        try:
            session.execute(
                text(
                    """
                    UPDATE public.live_plan_submissions
                    SET consumer_token = NULL, consumer_until = NULL
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND consumer_token = :token
                    """
                ),
                {
                    "plan_id": str(plan_id),
                    "step_no": int(step_no),
                    "token": str(token),
                },
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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
        merge_detail: bool = True,
        consumer_token: Optional[str] = None,
        db: Any = None,
    ) -> Dict[str, Any]:
        """Write an outcome. Never regresses a terminal row.

        ``merge_detail`` keeps the durable cursor/stage keys that a resumable
        finalize relies on; the lease holder may pass ``consumer_token`` to make
        the write conditional on still owning the step (so a consumer that lost
        its lease cannot overwrite the winner's progress).
        """
        owns = db is None
        session = db or self.session_factory()
        dialect = self._dialect(session)
        json_cast = ":{0}" if dialect == "sqlite" else "CAST(:{0} AS jsonb)"
        empty_json = "'[]'" if dialect == "sqlite" else "'[]'::jsonb"
        if merge_detail and dialect != "sqlite":
            detail_expr = f"COALESCE(detail, '{{}}'::jsonb) || {json_cast.format('detail')}"
        elif merge_detail:
            detail_expr = f"json_patch(COALESCE(detail, '{{}}'), {json_cast.format('detail')})"
        else:
            detail_expr = json_cast.format("detail")
        orders_expr = (
            f"CASE WHEN {json_cast.format('broker_order_ids')} = {empty_json} "
            f"THEN broker_order_ids ELSE {json_cast.format('broker_order_ids')} END"
        )
        now_expr = "CURRENT_TIMESTAMP" if dialect == "sqlite" else "NOW()"
        guard = ""
        params: Dict[str, Any] = {
            "plan_id": str(plan_id),
            "step_no": int(step_no),
            "state": str(state),
            "broker_order_ids": json.dumps(list(broker_order_ids)),
            "detail": json.dumps(dict(detail or {})),
        }
        if consumer_token is not None:
            # The lease must still be VALID, not merely named: a slow owner whose
            # lease expired (and whose step another consumer has taken over) must
            # not be able to write an outcome for it.
            guard = (
                " AND consumer_token = :consumer_token"
                f" AND consumer_until IS NOT NULL AND consumer_until > {now_expr}"
            )
            params["consumer_token"] = str(consumer_token)
        try:
            session.execute(
                text(
                    f"""
                    UPDATE public.live_plan_submissions
                    SET state = :state,
                        broker_order_ids = {orders_expr},
                        detail = {detail_expr},
                        updated_at = {now_expr}
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state NOT IN ('filled', 'rejected', 'no_op')
                      {guard}
                    """
                ),
                params,
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
        live_option_chain_max_age_seconds: float = LIVE_OPTION_CHAIN_MAX_AGE_SECONDS,
        submissions: Any = None,
        sweep_lease_seconds: float = 120.0,
        position_reader: Any = None,
        authority_reader: Any = None,
        tick_reader: Any = None,
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
        #: The instrument catalog's broker TICK for one frozen leg. A price can
        #: only be placed on the broker's own grid, so an unknown tick refuses
        #: (``LIVE_LIMIT_TICK_UNKNOWN``) rather than guessing one.
        self.tick_reader = tick_reader or (
            lambda plan, leg: live_catalog_tick_size(
                plan, leg, session_factory=session_factory
            )
        )
        self._clock = clock or _utcnow
        self.quote_max_age_seconds = float(quote_max_age_seconds)
        self.live_option_chain_max_age_seconds = float(live_option_chain_max_age_seconds)
        #: Durable per-step claim/outcome: the ONLY submission state.
        self.submissions = submissions or LiveSubmissionStore(session_factory=session_factory)
        #: The timeout sweep takes the same durable single-writer fence as the
        #: outcome consumer. A short lease would let a slow broker call lose the
        #: right to write while still holding a broker-side cancel.
        self.sweep_lease_seconds = float(sweep_lease_seconds)
        #: The DOMAIN seams (roll state machine, durable option run binding and
        #: the option engine's own step derivation) are reused through the paper
        #: executor's existing methods rather than re-implemented here. Built
        #: lazily: a single-leg live plan never pays for the option store.
        self._domain_executor = None

    def _domain(self) -> Any:
        if self._domain_executor is None:
            from .execution import PaperPlanExecutor

            self._domain_executor = PaperPlanExecutor(
                session_factory=self.session_factory, clock=self._clock
            )
        return self._domain_executor

    def _attributed_quantity(self, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> int:
        """One leg's attributed CURRENT quantity, from the platform reader.

        A lane whose step is an ABSOLUTE FLAT (a roll's old-contract close) sizes
        what it closes from the book, so an unreadable book refuses rather than
        becoming a zero-quantity no-op.
        """
        plan_id = str(plan.get("plan_id") or "")
        if self.position_reader is None:
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "message": "no authoritative attributed-current reader"},
            )
        try:
            return int(self.position_reader(plan=dict(plan), leg=dict(leg)) or 0)
        except LiveRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "error": str(exc)},
            ) from exc

    def _check_roll_peer_book(
        self, plan: Mapping[str, Any], *, release: bool = False
    ) -> None:
        """Refuse a roll whose frozen peer no longer matches attributed truth.

        The peer is the other contract a roll is expected to carry. Trusting its
        caller-supplied quantity lets a stale payload hide twice the book and,
        on release, close that larger book behind too little replacement.
        """
        resolved = plan.get("resolved_plan") or {}
        roll_role = str((resolved.get("roll") or {}).get("role") or "")
        wanted_role = "close_old" if release else "open_new"
        if (
            str(resolved.get("target_kind") or "") != "target_futures"
            or roll_role != wanted_role
        ):
            return
        mismatches = []
        for peer in resolved.get("old_legs") or []:
            try:
                expected = int(peer.get("signed_quantity"))
            except (TypeError, ValueError) as exc:
                raise LiveRefusal(
                    "ROLL_PEER_MISMATCH",
                    {"plan_id": plan.get("plan_id"), "reason": str(exc)},
                ) from exc
            current = self._attributed_quantity(plan, dict(peer))
            if current != expected:
                mismatches.append(
                    {
                        "instrument_id": str(peer.get("instrument_id") or ""),
                        "expected_quantity": expected,
                        "attributed_quantity": current,
                    }
                )
        if mismatches:
            raise LiveRefusal(
                "ROLL_PEER_MISMATCH",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "roll_peer_mismatches": mismatches,
                    "message": (
                        "The frozen roll peer does not match the strategy's current "
                        "attributed book"
                    ),
                },
            )

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _legs(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
        return [dict(leg) for leg in ((plan.get("resolved_plan") or {}).get("legs") or [])]

    # -- lane gates ---------------------------------------------------------

    def _check_lane_preconditions(
        self, plan: Mapping[str, Any], lane: str, *, binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """The lane's own admission gates, BEFORE anything is materialized.

        The parent protocol is lane-agnostic, but a lane's domain invariants are
        not: a roll half must belong to a real roll of this strategy and account
        (and may not quietly close an open roll's old leg), and an option
        structure must resolve to its durable run inside its frozen expiry policy.
        These are the SAME rules the paper lane enforces - reused, not restated.
        """
        from .live_sequence import LANE_FUTURES_ROLL, LANE_OPTION_STRUCTURE

        if lane == LANE_FUTURES_ROLL:
            return {"roll": self._check_roll_gates(plan, require_release=False)}
        if lane == LANE_OPTION_STRUCTURE:
            return {"option": self._check_option_gates(plan, binding=binding)}
        return {}

    def _check_roll_gates(
        self, plan: Mapping[str, Any], *, require_release: bool
    ) -> Dict[str, Any]:
        """Reuse the paper executor's roll contract, with the release gate optional.

        ``require_release=False`` is the SUBMIT-time reading: a ``close_old`` plan
        is materialized ``withheld`` while the roll is still acquiring, so the
        "close not released yet" refusal is the expected state rather than a
        reason to refuse the plan. Every identity, account and contract check
        still runs - and the release pass re-reads them with
        ``require_release=True`` before it may place anything.
        """
        from .execution import ExecutionRefusal, PaperPlanExecutor

        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        try:
            ref = PaperPlanExecutor._roll_binding(plan)
        except ExecutionRefusal as exc:
            raise LiveRefusal(exc.reason_code, exc.detail) from exc
        if not ref:
            # An unbound plan may still not close a contract an OPEN roll holds:
            # there is no "optional" roll bypass.
            try:
                self._domain()._refuse_ungated_roll_close(
                    plan, strategy_id=strategy_id, account_id=account_id
                )
            except ExecutionRefusal as exc:
                raise LiveRefusal(exc.reason_code, exc.detail) from exc
            return {"role": None, "roll_id": None}
        try:
            roll = self._domain()._roll_preconditions(plan, ref)
        except ExecutionRefusal as exc:
            if not require_release and str(exc.reason_code) == "ROLL_CLOSE_NOT_RELEASED":
                return {
                    "role": str(ref.get("role") or ""),
                    "roll_id": ref.get("roll_id"),
                    "roll_state": str((exc.detail or {}).get("roll_state") or ""),
                    "proven_filled_quantity": (exc.detail or {}).get("proven_filled_quantity"),
                    "required_replacement_quantity": (exc.detail or {}).get(
                        "required_replacement_quantity"
                    ),
                    "release_pending": True,
                }
            raise LiveRefusal(exc.reason_code, exc.detail) from exc
        return {
            "role": str(ref.get("role") or ""),
            "roll_id": str(roll.get("roll_id") or ""),
            "roll_state": str(roll.get("state") or ""),
            "proven_filled_quantity": int(roll.get("proven_filled_quantity") or 0),
            "required_replacement_quantity": int(
                roll.get("required_replacement_quantity") or 0
            ),
        }

    def _option_target(
        self, plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """The durable option run this frozen structure executes against."""
        from .execution import ExecutionRefusal

        try:
            return self._domain()._resolve_option_target(dict(plan), dict(binding))
        except ExecutionRefusal as exc:
            raise LiveRefusal(exc.reason_code, exc.detail) from exc

    def _option_steps(
        self, plan: Mapping[str, Any], target: Mapping[str, Any]
    ) -> List[Any]:
        """The existing option engine's OWN step derivation for this run."""
        from .execution import ExecutionRefusal

        try:
            # ``target`` is passed THROUGH, never copied: the engine's derivation
            # records the roll context (``_adjust_roll*``) and the desired run legs
            # on the target it is given, and the lane builder reads them back. A
            # copy would silently drop the roll boundary and release the old
            # generation beside the new one.
            return list(self._domain()._option_run_steps(dict(plan), target))
        except ExecutionRefusal as exc:
            raise LiveRefusal(exc.reason_code, exc.detail) from exc

    def _begin_option_run(
        self, plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Take OWNERSHIP of this option run's next transition, before any submit.

        The transition is the existing engine's own compare-and-set on the run's
        observed status, so two plans that target the same run can never both move
        it: exactly one wins and the loser refuses. A run left ``exiting`` by an
        unknown outcome is refused outright, so a restart never repeats an exit.
        """
        from .execution import ExecutionRefusal

        target = self._option_target(plan, binding)
        try:
            return self._domain()._begin_option_run(
                dict(target),
                plan_id=str(plan.get("plan_id") or ""),
                actor="live-adapter",
            )
        except ExecutionRefusal as exc:
            raise LiveRefusal(exc.reason_code, exc.detail) from exc

    def _check_option_gates(
        self, plan: Mapping[str, Any], *, binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Resolve the run and enforce its frozen policy BEFORE any submission.

        Two existing adapters are actually CALLED here rather than cited:

        * the plan/run binding (``resolve_plan_option_run``) - so a structure can
          only ever execute against the durable run the platform bound it to, and
          an exit's reference is validated against that run's own leg identities;
        * the frozen EXPIRY POLICY (``OptionExpiryPolicy.check``) - an entry that
          would open exposure inside the cutoff window under
          ``exit_before_cutoff`` is refused by name, because the platform has no
          mandate to open a structure it would immediately have to escalate.
        """
        target = self._option_target(plan, binding)
        run = target.get("run")
        phase = str(target.get("phase") or "")
        resolved = dict(plan.get("resolved_plan") or {})
        product = str(resolved.get("product") or "NRML").upper()
        expiry = resolved.get("expiry")
        detail: Dict[str, Any] = {
            "phase": phase,
            "option_run_id": str(getattr(run, "strategy_run_id", "") or ""),
            "option_run_status": str(getattr(run, "status", "") or ""),
            "worker_run_id": str(binding.get("strategy_run_id") or ""),
            "expiry": None if expiry is None else str(expiry),
            "expiry_policy": str(resolved.get("expiry_policy") or ""),
        }
        if phase == "entry" and str(product) != "MIS":
            from backend.options.protection.expiry_policy import OptionExpiryPolicy

            check = OptionExpiryPolicy().check(
                account_id=str(plan.get("account_id") or ""),
                run={
                    "strategy_run_id": detail["option_run_id"],
                    "expiry_policy": detail["expiry_policy"],
                    "status": detail["option_run_status"],
                },
                expiry=expiry,
                product=product,
                now=self._clock(),
                notify=False,
            )
            detail["expiry_check"] = {
                "reason": check.reason,
                "escalated": bool(check.escalated),
                "action_required": bool(check.action_required),
                "days_to_expiry": check.days_to_expiry,
                "policy": str(check.policy or ""),
            }
            if check.action_required:
                raise LiveRefusal(
                    "OPTION_EXPIRY_CUTOFF_PASSED",
                    {
                        "plan_id": str(plan.get("plan_id") or ""),
                        "instrument_id": "",
                        **detail,
                        "message": (
                            "the frozen structure exits before the expiry cutoff and this "
                            "entry is already inside the window; the platform does not open "
                            "what it would immediately have to escalate"
                        ),
                    },
                )
        return detail

    @classmethod
    def _single_leg(cls, plan: Mapping[str, Any]) -> Dict[str, Any]:
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
        legs = cls._legs(plan)
        if len(legs) != 1:
            raise LiveRefusal(
                "LIVE_PLAN_COMPOUND_UNSUPPORTED",
                {"plan_id": plan_id, "plan_kind": plan_kind, "leg_count": len(legs)},
            )
        return legs[0]

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

    def _check_approval(
        self,
        plan: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """The approval must hold on EVERY pin. Strict by construction."""
        approval, mismatched, detail = self._approval_pin_state(plan)
        if mismatched:
            raise LiveRefusal(
                "LIVE_APPROVAL_INVALID",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "approval_id": str(approval.get("approval_id") or ""),
                    "mismatched_pins": mismatched,
                    "detail": detail,
                },
            )
        self._check_approval_binding(plan, approval)
        return approval

    @staticmethod
    def _is_option_plan(plan: Mapping[str, Any]) -> bool:
        resolved = plan.get("resolved_plan") or {}
        if not isinstance(resolved, Mapping):
            resolved = {}
        return (
            str(resolved.get("target_kind") or "") == "option_structure"
            or str(plan.get("plan_kind") or "") == "option_structure"
        )

    def _check_approval_binding(
        self, plan: Mapping[str, Any], approval: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """The VERSION and OPTION-GENERATION pins this approval froze (C1.2 S3).

        A plan hash still matching is not enough: the same logical plan can be
        recompiled under a moved strategy version, a re-mapped catalog generation
        or a moved option structure, and trading the frozen artifact against any
        of those is exactly what the owner did not authorise. Each divergence is
        its OWN named refusal, so the operator learns which input moved.

        Not-pinned is not a mismatch: a legacy approval (or a run with no owner
        row at approval time) binds nothing, and binding nothing must never read
        as "cleared". A reduce-only exit remains admissible with an unknown owner
        for the same reason - only a pin the owner DID grant is enforced.
        """
        plan_id = str(plan.get("plan_id") or "")
        approved_version = {
            "strategy_version_id": approval.get("strategy_version_id"),
            "version_number": approval.get("version_number"),
            "source_sha256": approval.get("source_sha256"),
            "policy_hash": approval.get("policy_hash"),
        }
        proof: Dict[str, Any] = {}
        if any(value not in (None, "") for value in approved_version.values()):
            current_version = dict(
                self.approvals.version_binding_for_plan(plan) or {}
            )
            changed = {
                key: {"approved": value, "current": current_version.get(key)}
                for key, value in approved_version.items()
                if value not in (None, "")
                and str(current_version.get(key)) != str(value)
            }
            if changed:
                raise LiveRefusal(
                    "LIVE_APPROVAL_VERSION_CHANGED",
                    {
                        "plan_id": plan_id,
                        "approval_id": str(approval.get("approval_id") or ""),
                        "changed": changed,
                        "message": (
                            "the strategy version, source or policy this approval was "
                            "bound to no longer matches the plan's persisted version; "
                            "the changed artifact needs a new plan and approval"
                        ),
                    },
                )
            proof["version_binding"] = dict(approved_version)

        approved_run = str(approval.get("option_run_id") or "")
        if not self._is_option_plan(plan) or not approved_run:
            return proof

        state = dict(self.approvals.option_binding_state(plan) or {})
        if str(state.get("option_run_id") or "") != approved_run:
            raise LiveRefusal(
                "OPTION_ADJUSTMENT_STALE_BASIS",
                {
                    "plan_id": plan_id,
                    "approval_id": str(approval.get("approval_id") or ""),
                    "approved_option_run_id": approved_run,
                    "current_option_run_id": str(state.get("option_run_id") or ""),
                    "message": "the frozen option target no longer names this approval's run",
                },
            )

        current_catalog = str(state.get("catalog_generation") or "")
        approved_catalog = str(approval.get("catalog_generation") or "")
        if current_catalog and current_catalog != approved_catalog:
            raise LiveRefusal(
                "LIVE_OPTION_CATALOG_GENERATION_CHANGED",
                {
                    "plan_id": plan_id,
                    "approval_id": str(approval.get("approval_id") or ""),
                    "approved_catalog_generation": approved_catalog,
                    "current_catalog_generation": current_catalog,
                    "message": (
                        "every option leg is a pinned derivative contract, so a moved "
                        "catalog generation invalidates the approval even when the "
                        "pinned coordinates re-resolve"
                    ),
                },
            )

        reserved = approval.get("reserved_option_generation")
        if reserved is not None:
            held = state.get("structure_generation")
            if held is None:
                raise LiveRefusal(
                    "OPTION_ADJUSTMENT_STALE_BASIS",
                    {
                        "plan_id": plan_id,
                        "approval_id": str(approval.get("approval_id") or ""),
                        "option_run_id": approved_run,
                        "reserved_option_generation": int(reserved),
                        "reason": "OPTION_RUN_UNREADABLE",
                        "error": state.get("unreadable"),
                    },
                )
            if int(held) != int(reserved):
                raise LiveRefusal(
                    "OPTION_ADJUSTMENT_STALE_BASIS",
                    {
                        "plan_id": plan_id,
                        "approval_id": str(approval.get("approval_id") or ""),
                        "option_run_id": approved_run,
                        "reserved_option_generation": int(reserved),
                        "structure_generation": int(held),
                        "message": (
                            "the run has moved to a different leg generation than the "
                            "one this approval owns; the run is never re-derived "
                            "against a newer structure"
                        ),
                    },
                )

        approved_policy = approval.get("protection_policy_version")
        if approved_policy:
            if state.get("unreadable"):
                raise LiveRefusal(
                    "OPTION_PROTECTION_OWNER_CONFLICT",
                    {
                        "plan_id": plan_id,
                        "approval_id": str(approval.get("approval_id") or ""),
                        "option_run_id": approved_run,
                        "reason": "OPTION_PROTECTION_OWNER_UNREADABLE",
                        "error": state.get("unreadable"),
                    },
                )
            current_policy = state.get("protection_policy_version")
            if not current_policy:
                raise LiveRefusal(
                    "OPTION_PROTECTION_OWNER_CONFLICT",
                    {
                        "plan_id": plan_id,
                        "approval_id": str(approval.get("approval_id") or ""),
                        "option_run_id": approved_run,
                        "approved_protection_policy_version": str(approved_policy),
                        "reason": "OPTION_PROTECTION_OWNER_UNKNOWN",
                        "message": (
                            "the approval pinned a protection owner policy and the run "
                            "no longer has a readable owner row"
                        ),
                    },
                )
            if str(current_policy) != str(approved_policy):
                raise LiveRefusal(
                    "OPTION_PROTECTION_POLICY_CHANGED",
                    {
                        "plan_id": plan_id,
                        "approval_id": str(approval.get("approval_id") or ""),
                        "option_run_id": approved_run,
                        "approved_protection_policy_version": str(approved_policy),
                        "current_protection_policy_version": str(current_policy),
                    },
                )
        proof["option_binding"] = {
            "option_run_id": approved_run,
            "based_on_generation": approval.get("based_on_generation"),
            "reserved_option_generation": reserved,
            "protection_policy_version": approved_policy,
        }
        return proof

    def _approval_pin_state(
        self, plan: Mapping[str, Any]
    ) -> tuple[Dict[str, Any], List[str], Optional[Any]]:
        """The live approval and EVERY pin it currently fails, unfiltered."""
        plan_id = str(plan.get("plan_id") or "")
        approval = self.approvals.active_for_plan(plan_id)
        if approval is None:
            raise LiveRefusal("LIVE_APPROVAL_REQUIRED", {"plan_id": plan_id})
        validity = self.approvals.structural_validity(plan, approval, now=self._clock())
        return (
            dict(approval),
            [str(pin) for pin in (validity.get("mismatched_pins") or [])],
            validity.get("detail"),
        )

    def _exposure_move_is_own_fills(
        self,
        plan: Mapping[str, Any],
        specs: Sequence[Any],
        parent: Mapping[str, Any],
    ) -> tuple[bool, Dict[str, Any]]:
        """Whether the book moved ONLY by this parent's OWN confirmed legs.

        Per instrument the identity is

        ``current_attributed == frozen_current_at_admission + Σ own filled delta``

        where the frozen baseline is the ``current_quantity`` recorded in this
        parent's immutable step specification and the own filled delta comes from
        the parent's recorded per-leg outcomes (the VERIFIED fill evidence, not the
        order acknowledgement). Any other movement on the instrument - another plan
        of the same run, a manual trade, a corporate action - breaks the identity,
        and then the frozen delta provably no longer describes the book: the
        release is refused by name and the operator re-approves.
        """
        legs_detail = dict((dict(parent.get("detail") or {})).get("legs") or {})
        baseline: Dict[tuple, int] = {}
        own_delta: Dict[tuple, int] = {}
        legs_by_key: Dict[tuple, Any] = {}
        for spec in specs:
            key = (str(spec.instrument_id), str(spec.product))
            baseline.setdefault(key, int(spec.current_quantity))
            legs_by_key.setdefault(key, spec)
            entry = dict(legs_detail.get(str(int(spec.step_no))) or {})
            sign = 1 if str(spec.side).upper() == "BUY" else -1
            own_delta[key] = own_delta.get(key, 0) + sign * int(
                entry.get("filled_quantity") or 0
            )
        mismatches: List[Dict[str, Any]] = []
        for key, base in sorted(baseline.items()):
            spec = legs_by_key[key]
            leg_view = self._leg_view(spec)
            try:
                actual = int(self.position_reader(plan=dict(plan), leg=dict(leg_view)) or 0)
            except Exception as exc:  # noqa: BLE001 - unreadable evidence is not proof
                return False, {
                    "reason": "POSITION_EVIDENCE_UNAVAILABLE",
                    "error": str(exc),
                    "instrument_id": key[0],
                }
            expected = base + own_delta.get(key, 0)
            if actual != expected:
                mismatches.append(
                    {
                        "instrument_id": key[0],
                        "product": key[1],
                        "frozen_current_quantity": base,
                        "own_filled_delta": own_delta.get(key, 0),
                        "expected_quantity": expected,
                        "actual_quantity": actual,
                    }
                )
        return (not mismatches), {
            "identity": "frozen_current + own_filled_delta == current_attributed",
            "mismatches": mismatches,
            "legs_considered": len(baseline),
        }

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
        resolved_target_kind = str((plan.get("resolved_plan") or {}).get("target_kind") or "")
        if resolved_target_kind == "option_structure":
            try:
                validate_option_chain_evidence(
                    plan,
                    now=self._clock(),
                    max_age_seconds=self.live_option_chain_max_age_seconds,
                )
            except Exception as exc:
                from backend.options.market.freshness import OptionChainEvidenceRefusal

                if not isinstance(exc, OptionChainEvidenceRefusal):
                    raise
                raise LiveRefusal(exc.reason_code, exc.detail) from exc
        self._check_option_structure_admissibility(plan)
        from backend.strategies import daily_loss

        # The same two daily-loss controls the paper/live pipeline applies, with
        # LIVE evidence: the strategy's attributed realized P&L today, and the
        # account-wide day P&L behind the optional cap. Admission stays pure.
        evidence = daily_loss.admission_daily_loss_evidence(
            plan=plan,
            environment="live",
            session_factory=self.session_factory,
            now=self._clock(),
        )
        verdict = self.admission.evaluate(
            plan,
            execution_environment="live",
            now=self._clock(),
            margin_evidence=margin_evidence,
            catalog_state=catalog_state,
            **evidence,
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

    @staticmethod
    def staged_buy_max_price_drift_pct() -> float:
        """C1.1's drift bound, under its established name and default.

        The value and the bounded-LIMIT derivation it feeds are shared with the
        option lane through ``live_limit_orders``; this accessor keeps the C1.1
        name (and its env var) working.
        """
        return staged_buy_max_price_drift_pct()

    async def _staged_funding_gate(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        states: Mapping[int, str],
        quote: Optional[Mapping[str, Any]],
        quote_reader: Any,
        funds_reader: Optional[Callable[[], Any]],
        margin_evidence: Optional[Mapping[str, Any]],
        catalog_state: Optional[Mapping[str, Any]],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """The whole gate, stage 1 then stage 2, for a caller that holds no lock.

        ``release_step`` calls the two stages SEPARATELY, so the authoritative
        funds read happens while the reservation ACCOUNT lock is held; this
        composition is kept only for callers that need the gate in one call. It is
        NOT the authority when money can move in between - a funds figure read
        before the account lock can describe cash another plan has already spent.
        """
        validated_quote = await self._staged_funding_quote_stage(
            plan, spec, states=states, quote=quote, quote_reader=quote_reader
        )
        funds = self._staged_funding_funds_stage(
            plan,
            spec,
            funds_reader=funds_reader,
            margin_evidence=margin_evidence,
            catalog_state=catalog_state,
        )
        return validated_quote, funds

    async def _staged_funding_quote_stage(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        states: Mapping[int, str],
        quote: Optional[Mapping[str, Any]],
        quote_reader: Any,
    ) -> Dict[str, Any]:
        """STAGE 1 - sequencing and price evidence. Reads no money; takes no lock.

        A non-``filled`` reduction is sequencing evidence, and the quote/drift check
        bounds the buy's estimated cost against the frozen reference price. Neither
        spends account cash, so this stage may run before the account lock.
        """
        plan_id = str(plan.get("plan_id") or "")
        unconfirmed = [
            int(value)
            for value in (spec.depends_on or ())
            if str(states.get(int(value)) or "") != "filled"
        ]
        if unconfirmed:
            raise LiveRefusal(
                "STAGED_FUNDING_REDUCTION_NOT_CONFIRMED",
                {
                    "plan_id": plan_id,
                    "step_no": int(spec.step_no),
                    "confirmed_reduction_steps": [
                        int(value) for value in spec.depends_on if value not in unconfirmed
                    ],
                    "unconfirmed_reduction_steps": unconfirmed,
                    "states": {str(key): str(value) for key, value in states.items()},
                },
            )

        leg = self._leg_view(spec)
        if quote is None and callable(quote_reader):
            candidate = quote_reader(leg)
            if hasattr(candidate, "__await__"):
                candidate = await candidate
            quote = candidate
        validated_quote = self._check_quote(plan, leg, dict(quote or {}))

        reference_price = abs(float(getattr(spec, "detail", {}).get("reference_price") or 0.0))
        if reference_price <= 0.0:
            quantity = abs(int(spec.quantity))
            reference_price = (
                abs(float(spec.notional_inr)) / quantity if quantity else 0.0
            )
        if reference_price <= 0.0:
            raise LiveRefusal(
                "LIVE_REFERENCE_PRICE_UNAVAILABLE",
                {"plan_id": plan_id, "step_no": int(spec.step_no)},
            )
        price = abs(float(validated_quote.get("ltp") or 0.0))
        drift = abs(price / reference_price - 1.0) if reference_price else 1.0
        max_drift = self.staged_buy_max_price_drift_pct()
        if drift > max_drift:
            raise LiveRefusal(
                "LIVE_FINANCING_PRICE_DRIFT",
                {
                    "plan_id": plan_id,
                    "step_no": int(spec.step_no),
                    "reference_price_inr": reference_price,
                    "quote_price_inr": price,
                    "price_drift_pct": drift,
                    "max_price_drift_pct": max_drift,
                },
            )
        return validated_quote

    def _staged_funding_funds_stage(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        funds_reader: Optional[Callable[[], Any]],
        margin_evidence: Optional[Mapping[str, Any]],
        catalog_state: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """STAGE 2 - the authoritative funds read, the freshness bound and admission.

        This is the stage that decides whether the buy MAY spend, so its caller must
        already hold the reservation account lock (``release_step`` does): the read
        has to describe the account AFTER every competing authorization, spend or
        confirmed fill that lands while the lock is being waited for. A figure read
        before the lock is a torn view - it can still contain cash another plan has
        already spent - and is never used as the authority.
        """
        plan_id = str(plan.get("plan_id") or "")
        try:
            funds = (
                dict(funds_reader() or {})
                if callable(funds_reader)
                else dict(margin_evidence or {})
            )
        except LiveRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - a broker read failure is not headroom
            raise LiveRefusal(
                "STAGED_FUNDING_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "step_no": int(spec.step_no), "error": str(exc)},
            ) from exc
        account_scope = str(funds.get("account_scope") or "")
        if not funds or funds.get("usable") is None or (
            account_scope and account_scope != str(plan.get("account_id") or "")
        ):
            raise LiveRefusal(
                "STAGED_FUNDING_EVIDENCE_UNAVAILABLE",
                {
                    "plan_id": plan_id,
                    "step_no": int(spec.step_no),
                    "account_scope": account_scope or None,
                },
            )
        as_of = _as_datetime(funds.get("as_of"))
        age = (self._clock() - as_of).total_seconds() if as_of else None
        max_age = margin_max_age_seconds()
        if age is None or age > max_age:
            raise LiveRefusal(
                "STAGED_FUNDING_EVIDENCE_STALE",
                {
                    "plan_id": plan_id,
                    "step_no": int(spec.step_no),
                    "funds_age_seconds": age,
                    "max_age_seconds": max_age,
                },
            )
        self._check_admission(
            plan, margin_evidence=funds, catalog_state=catalog_state
        )
        return funds

    def _check_option_structure_admissibility(self, plan: Mapping[str, Any]) -> None:
        """Refuse a live option plan the strategy's own durable work blocks.

        The rules are the plan/run binding edge's own, one per frozen phase
        (``assess_option_entry_admissibility`` for an ENTRY,
        ``assess_option_adjust_admissibility`` for an ADJUST) - the SAME ones the
        paper admission and the execution-time gates apply - asked here because
        the live lane admits through its own service rather than
        ``pipeline.admit``. A run this plan itself is bound to does not block it,
        so the entry's own withheld steps can still be released. A live ADJUST is
        then expanded into release-ruled steps by ``build_option_steps`` (C1.2).
        """
        from backend.options.execution.plan_binding import (
            PlanBindingRefusal,
            assess_option_adjust_admissibility,
            assess_option_entry_admissibility,
            is_option_adjust_plan,
            is_option_entry_plan,
        )

        if is_option_entry_plan(plan):
            assess = assess_option_entry_admissibility
        elif is_option_adjust_plan(plan):
            assess = assess_option_adjust_admissibility
        else:
            # Every other lane and every option EXIT is untouched - answered here
            # so a per-step release never opens a session for this.
            return
        try:
            with self.session_factory() as session:
                assess(
                    plan,
                    strategy_id=str(plan.get("strategy_id") or ""),
                    account_id=str(plan.get("account_id") or ""),
                    execution_environment="live",
                    session=session,
                )
        except PlanBindingRefusal as exc:
            raise LiveRefusal(exc.reason_code, exc.detail) from exc

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

    def _weight_target(
        self, plan: Mapping[str, Any], leg: Mapping[str, Any], lot: int
    ) -> int:
        """A weight leg's executable target, from the basis FROZEN with the plan.

        Re-reading the current admission policy here would silently re-size an
        already-approved target, so the frozen ``capital_basis_inr`` is the only
        sizing input. The arithmetic is the ONE shared rule admission and paper
        also call (``financing.weight_target_quantity``: ``basis x (1 - buffer)``
        and a floor to the pinned lot), so the live, paper and admission answers
        can never disagree - a sub-lot weight sizes to ZERO rather than up to a
        lot nobody reserved or bought. The current policy allocation is read for
        DRIFT only: an authority that no longer covers the frozen basis refuses
        rather than executing against a limit that no longer exists.
        """
        from .financing import weight_target_quantity

        plan_id = str(plan.get("plan_id") or "")
        resolved = dict(plan.get("resolved_plan") or {})
        logical = dict(plan.get("logical_plan") or {})
        basis_raw = resolved.get("capital_basis_inr", logical.get("capital_basis_inr"))
        if basis_raw is None:
            raise LiveRefusal(
                "LIVE_TARGET_MISSING",
                {
                    "plan_id": plan_id,
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "message": (
                        "a weight-sized live plan must carry the capital basis it was "
                        "frozen with"
                    ),
                },
            )
        try:
            basis = float(basis_raw)
        except (TypeError, ValueError) as exc:
            raise LiveRefusal(
                "LIVE_TARGET_MISSING",
                {"plan_id": plan_id, "capital_basis_inr": str(basis_raw)},
            ) from exc
        if basis <= 0:
            raise LiveRefusal(
                "LIVE_TARGET_MISSING", {"plan_id": plan_id, "capital_basis_inr": basis}
            )
        buffer_raw = resolved.get("cash_buffer_pct", logical.get("cash_buffer_pct"))
        try:
            buffer_pct = 0.0 if buffer_raw is None else float(buffer_raw)
        except (TypeError, ValueError) as exc:
            raise LiveRefusal(
                "LIVE_TARGET_MISSING", {"plan_id": plan_id, "cash_buffer_pct": str(buffer_raw)}
            ) from exc
        price_raw = leg.get("reference_price")
        try:
            price = float(price_raw) if price_raw is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            raise LiveRefusal(
                "LIVE_REFERENCE_PRICE_UNAVAILABLE",
                {
                    "plan_id": plan_id,
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "message": "a weight-sized leg needs the reference price frozen with the plan",
                },
            )
        return int(
            weight_target_quantity(
                weight=float(leg.get("target_weight") or 0.0),
                capital=basis * max(0.0, 1.0 - buffer_pct),
                price=price,
                lot=int(lot or 1),
            )
        )

    def _resolve_delta(self, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> Dict[str, Any]:
        """The step's direction and quantity, from the ATTRIBUTED current position.

        The frozen leg carries either an absolute TARGET (``signed_quantity``) or
        a full-snapshot ``target_weight``. Without a current reading the size is
        unknown, and an unknown size refuses rather than becoming a default BUY of
        the target's magnitude.

        ``increases_exposure`` reuses the paper executor's own rule (the book grows
        OR the trade crosses flat), so the live lane cannot invent a laxer notion
        of "risk-increasing" than admission and the reservation already enforce.
        """
        from .execution import PaperPlanExecutor

        plan_id = str(plan.get("plan_id") or "")
        target_raw = leg.get("signed_quantity")
        long_only = False
        lot = self._pinned_units(plan, leg)
        if target_raw is not None:
            target = int(target_raw)
        elif leg.get("target_weight") is not None:
            target = self._weight_target(plan, leg, lot)
            # A full-snapshot weight is a long-only fraction: a sell REDUCES to
            # flat, and can never cross into a short.
            long_only = True
        else:
            raise LiveRefusal(
                "LIVE_TARGET_MISSING",
                {
                    "plan_id": plan_id,
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "message": "the leg names neither a signed quantity nor a target weight",
                },
            )
        if self.position_reader is None:
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {
                    "plan_id": plan_id,
                    "message": "no authoritative attributed-current reader is wired",
                },
            )
        try:
            current = int(self.position_reader(plan=dict(plan), leg=dict(leg)) or 0)
        except LiveRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "error": str(exc)},
            ) from exc
        delta = target - current
        if long_only and delta < 0:
            delta = max(delta, -current)
        quantity = abs(delta)
        if lot > 1:
            quantity = (quantity // lot) * lot
        side = "BUY" if delta > 0 else "SELL"
        price_raw = leg.get("reference_price")
        try:
            price = abs(float(price_raw)) if price_raw is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        return {
            "target": target,
            "current": current,
            "delta": delta,
            "side": side,
            "quantity": int(quantity),
            "lot_size": lot,
            "notional_inr": float(int(quantity) * price),
            "increases_exposure": bool(
                PaperPlanExecutor._opens_or_grows_exposure(target, current)
            ),
            "long_only": bool(long_only),
        }

    # -- submit -------------------------------------------------------------

    async def submit(
        self,
        plan: Mapping[str, Any],
        *,
        actor: str,
        run_binding: Mapping[str, Any],
        evaluation_authority: Mapping[str, Any],
        quote: Optional[Mapping[str, Any]] = None,
        margin_evidence: Optional[Mapping[str, Any]] = None,
        catalog_state: Optional[Mapping[str, Any]] = None,
        session_id: Optional[str] = None,
        lane: Optional[str] = None,
        step_specs: Optional[Sequence[Any]] = None,
        sequence: Any = None,
        quote_reader: Any = None,
    ) -> LiveSubmission:
        """Validate every control, materialize the durable parent, dispatch the ready steps.

        ONE frozen plan maps to ONE durable parent (``live_plan_executions``,
        ``UNIQUE (plan_id)``) whose ordered step/dependency specification is frozen
        here, at FIRST admission. The parent, every per-leg claim and the per-step
        barrier work are written in ONE transaction on the canonical book lock, so
        a crash leaves either the whole protocol or none of it - and a concurrent
        second executor reads the winner's rows instead of dispatching again.

        A step with prerequisites is materialized as ``withheld`` in-flight work
        and is NOT dispatched here: only the shared sequence pass may release it,
        and only while its prerequisites are filled and the persisted authority
        still holds.
        """
        from .live_sequence import (
            LaneContext,
            LivePlanSequence,
            StepSpec,
            build_steps,
            capacity_covers,
            lane_for_plan,
        )

        plan_id = str(plan.get("plan_id") or "")
        resolved_lane = str(lane or lane_for_plan(plan))
        legs = self._legs(plan)
        if not legs:
            raise LiveRefusal(
                "LIVE_PLAN_COMPOSITION_EMPTY",
                {"plan_id": plan_id, "plan_kind": str(plan.get("plan_kind") or "")},
            )
        binding = self._check_binding(plan, run_binding)
        self._check_authority(plan, evaluation_authority, binding)
        self._check_approval(plan)
        reservation = self._check_reservation(plan)
        admission = self._check_admission(
            plan, margin_evidence=margin_evidence, catalog_state=catalog_state
        )
        # ADMISSION owns the staged-lane classification (it recorded
        # ``staged_increase_inr``). The frozen step protocol must carry it so the
        # dependent buys of a staged plan go behind the staged funding gate rather
        # than the generic prerequisite rule.
        staged_financing = (
            (admission.get("detail") or {}).get("staged_increase_inr") is not None
        )
        # The LANE's own domain invariants (the roll contract, the durable option
        # run inside its frozen expiry policy) are checked once more here, before
        # anything is materialized. A lane that cannot name its domain object
        # refuses instead of freezing a protocol it could never dispatch.
        lane_gates = self._check_lane_preconditions(plan, resolved_lane, binding=binding)

        if step_specs is None:
            specs = build_steps(
                LaneContext(
                    plan=plan,
                    binding=binding,
                    authority=evaluation_authority,
                    execution_id="",
                    size_leg=lambda leg: self._resolve_delta(plan, dict(leg)),
                    attributed_quantity=lambda leg: self._attributed_quantity(plan, dict(leg)),
                    option_target=self._option_target,
                    option_run_steps=self._option_steps,
                    staged_financing=staged_financing,
                ),
                resolved_lane,
            )
        else:
            specs = [
                item if isinstance(item, StepSpec) else StepSpec.from_dict(dict(item))
                for item in step_specs
            ]
        if not specs:
            raise LiveRefusal("LIVE_PLAN_COMPOSITION_EMPTY", {"plan_id": plan_id})
        # Per-leg capacity accounting: the reservation must cover EVERY
        # pending/unsubmitted increasing leg, not merely the one dispatched first.
        covered, coverage = capacity_covers(reservation, specs)
        if not covered:
            raise LiveRefusal(
                "LIVE_CAPACITY_SHORTFALL",
                {
                    "plan_id": plan_id,
                    "lane": resolved_lane,
                    **coverage,
                    "message": (
                        "the reservation must cover every increasing leg of the parent; "
                        "the whole reservation is not spent on the first leg"
                    ),
                },
            )
        if quote is not None and len(specs) == 1:
            # The single-leg path validates the caller's quote up front (the
            # multi-leg paths read a fresh quote per leg at dispatch).
            self._check_quote(plan, legs[0], quote)

        account_id = str(plan.get("account_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        from .live_sequence import LANE_OPTION_STRUCTURE, RULE_IMMEDIATE

        if resolved_lane == LANE_OPTION_STRUCTURE and any(int(spec.quantity) for spec in specs):
            # The durable RUN must never read as "created" while orders are in
            # flight, so its transition is taken BEFORE the first submission. It is
            # a compare-and-set on the run's observed status: a second plan for the
            # same run (a duplicate entry, a second exit) refuses instead of racing.
            self._begin_option_run(plan, binding)
        sequence = sequence or LivePlanSequence(
            session_factory=self.session_factory,
            submissions=self.submissions,
            barrier=self.barrier,
        )
        claim_session = self.session_factory()
        try:
            self.barrier.lock_book(
                claim_session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment="live",
            )
            self._check_roll_peer_book(plan)
            _parent, created = sequence.materialize(
                plan=plan,
                lane=resolved_lane,
                step_specs=specs,
                detail={"actor": actor, "lane_gates": dict(lane_gates or {})},
                db=claim_session,
            )
            if not created:
                # Already materialized (pending/withheld/uncertain/rejected/no_op):
                # report the DURABLE state, never a second dispatch. A withheld
                # dependent is released only by the sequence pass, on evidence.
                claim_session.rollback()
                return self._submission_view(plan_id, specs)
            # Re-read the exposure-increasing authority and the evaluation
            # authority INSIDE the claim transaction: a revocation or expiry
            # between validation and dispatch must roll the whole parent back.
            self._recheck_at_dispatch(plan, evaluation_authority, binding)
            claim_session.commit()
        except LiveRefusal:
            claim_session.rollback()
            raise
        except Exception:
            claim_session.rollback()
            raise
        finally:
            claim_session.close()

        ready = [
            spec
            for spec in specs
            if not spec.depends_on
            and spec.quantity
            and str(spec.release_rule) == RULE_IMMEDIATE
        ]
        if ready:
            # The parent leaves ``planned`` as soon as a leg is actually sent; a
            # plan whose every leg is withheld stays ``planned`` and in flight.
            sequence.mark_executing(plan_id=plan_id)
        for spec in ready:
            await self.dispatch_step(
                plan,
                spec,
                binding=binding,
                account_id=account_id,
                strategy_id=strategy_id,
                actor=actor,
                session_id=session_id,
                quote=quote if len(specs) == 1 else None,
                quote_reader=quote_reader,
            )
        return self._submission_view(plan_id, specs)

    # -- per-step dispatch --------------------------------------------------

    @staticmethod
    def _leg_view(spec: Any) -> Dict[str, Any]:
        return {
            "instrument_id": str(getattr(spec, "instrument_id", "") or ""),
            "exchange": str(getattr(spec, "exchange", "") or ""),
            "tradingsymbol": str(getattr(spec, "tradingsymbol", "") or ""),
            "broker_exchange": str(getattr(spec, "broker_exchange", "") or ""),
            "broker_symbol": str(getattr(spec, "broker_symbol", "") or ""),
            "product": str(getattr(spec, "product", "") or ""),
            "variety": str(getattr(spec, "variety", "") or "regular"),
        }

    def _tick_for_step(self, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> tuple[float, str]:
        """The broker tick for one gated leg, and where it came from.

        The frozen leg answers first (the futures compiler records its tick);
        otherwise the instrument catalog is read. An unknown tick refuses: a
        price that is not on the broker's own grid is not a price we may send.
        """
        plan_id = str(plan.get("plan_id") or "")
        own = leg.get("tick_size")
        try:
            own_value = float(own) if own not in (None, "") else 0.0
        except (TypeError, ValueError):
            own_value = 0.0
        if own_value > 0.0:
            return own_value, "leg"
        if not callable(self.tick_reader):
            raise LiveRefusal(
                LIVE_LIMIT_TICK_UNKNOWN,
                {"plan_id": plan_id, "instrument_id": str(leg.get("instrument_id") or "")},
            )
        try:
            value = self.tick_reader(plan, leg)
        except LiveRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable catalog is UNKNOWN
            raise LiveRefusal(
                LIVE_LIMIT_TICK_UNKNOWN,
                {
                    "plan_id": plan_id,
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "error": str(exc),
                },
            ) from exc
        try:
            tick = float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            tick = 0.0
        if tick <= 0.0:
            raise LiveRefusal(
                LIVE_LIMIT_TICK_UNKNOWN,
                {
                    "plan_id": plan_id,
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "tick_size": None if value is None else str(value),
                },
            )
        return tick, "catalog"

    def _gated_execution_order(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        leg: Mapping[str, Any],
        side: str,
        quantity: int,
        quote: Optional[Mapping[str, Any]],
        session_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """The bounded-LIMIT ``execution_order`` block for a GATED leg, or ``None``.

        ``None`` means the leg is not gated (an immediate leg, or a MIS
        square-off released by the platform clock): it keeps its current
        behaviour. A gated leg is always a LIMIT, and the whole derivation is
        recorded so the durable step detail carries the frozen reference, the
        observed quote, the chosen price, the band and the tick source.
        """
        rule = str(getattr(spec, "release_rule", "") or "")
        if not is_gated_limit_step(lane=str(getattr(spec, "lane", "") or ""), spec=spec):
            return None
        plan_id = str(plan.get("plan_id") or "")
        step_no = int(getattr(spec, "step_no", 0) or 0)
        ordered = abs(int(quantity or 0))
        lane = str(getattr(spec, "lane", "") or "")
        detail = dict(getattr(spec, "detail", {}) or {})
        reference = frozen_reference_price(
            detail=detail,
            notional_inr=getattr(spec, "notional_inr", 0.0),
            quantity=ordered,
        )
        reference_source = "frozen_plan" if reference > 0.0 else ""
        if reference <= 0.0:
            # A RELEASE step is built from the run's OWN legs (``_removal_leg``),
            # and those carry no frozen plan price: the structure they close was
            # opened by a different plan. The fresh quote's LTP then stands in as
            # the reference - the same anchor the bounded derivation already applies when
            # the required side of the book is absent - so the leg is STILL a
            # bounded LIMIT and never an unbounded market order.
            raw_ltp = (quote or {}).get("ltp")
            try:
                candidate = abs(float(raw_ltp)) if raw_ltp is not None else 0.0
            except (TypeError, ValueError):
                candidate = 0.0
            if candidate > 0.0:
                reference = candidate
                reference_source = "live_quote"
        if reference <= 0.0:
            # A roll release is derived at release time, so its legacy step may
            # not carry a frozen reference. Fresh LTP is the fallback evidence:
            # derive_bounded_limit still clamps book evidence into this band.
            ltp = quote_reference_ltp(quote)
            if lane == "option_structure" and ltp:
                reference = ltp
        if reference <= 0.0:
            raise LiveRefusal(
                "LIVE_REFERENCE_PRICE_UNAVAILABLE",
                {
                    "plan_id": plan_id,
                    "step_no": step_no,
                    "release_rule": rule,
                    "lane": lane,
                    "quote_ltp": None if not quote else quote.get("ltp"),
                },
            )
        max_drift_env = (
            "LIVE_OPTION_LIMIT_MAX_DRIFT_PCT"
            if lane == "option_structure"
            else "LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT"
        )
        max_drift = (
            option_limit_max_drift_pct()
            if lane == "option_structure"
            else staged_buy_max_price_drift_pct()
        )
        tick, tick_source = self._tick_for_step(plan, leg)
        try:
            derived = derive_bounded_limit(
                side,
                reference,
                dict(quote or {}),
                max_drift,
                tick,
                tick_source=tick_source,
            )
        except LimitOrderRefusal as exc:
            raise LiveRefusal(
                exc.reason_code,
                {"plan_id": plan_id, "step_no": step_no, "lane": lane, "release_rule": rule, **exc.detail},
            ) from exc
        return {
            **derived,
            "lane": lane,
            "release_rule": rule,
            "reference_price_source": reference_source,
            "quantity": int(ordered),
            "variety": str(getattr(spec, "variety", "") or "regular"),
            "max_drift_env": max_drift_env,
            "session_id": str(session_id or ""),
            "submitted_at": self._clock().isoformat(),
        }

    async def dispatch_step(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        binding: Mapping[str, Any],
        account_id: str,
        strategy_id: str,
        actor: str = "",
        session_id: Optional[str] = None,
        quote: Optional[Mapping[str, Any]] = None,
        quote_reader: Any = None,
        authority: Optional[Mapping[str, Any]] = None,
        released_by: Optional[str] = None,
        extra_evidence: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send ONE already-claimed step through the existing intent handler.

        The claim is durable BEFORE this call: an accepted order is ``pending``, a
        transport failure or a response naming no order is ``uncertain`` (work and
        capacity retained, NEVER auto-repeated), and only an explicit authoritative
        refusal resolves the known-unfilled residual.
        """
        from .live_sequence import RULE_MIS_SQUAREOFF
        from .live_sequence import RULE_ROLL_CLOSE_RELEASED

        plan_id = str(plan.get("plan_id") or "")
        step_no = int(spec.step_no)
        step_ref = str(spec.step_ref)
        leg = self._leg_view(spec)
        quote = quote if quote is not None else (
            quote_reader(leg) if callable(quote_reader) else None
        )
        if quote is not None:
            self._check_quote(plan, leg, quote)
        elif spec.quantity:
            raise LiveRefusal("LIVE_QUOTE_MISSING", {"plan_id": plan_id, "step": step_no})

        delta = dict(getattr(spec, "detail", {}).get("sizing") or {})
        delta.setdefault("target", int(spec.target_quantity))
        delta.setdefault("current", int(spec.current_quantity))
        delta.setdefault("delta", int(spec.delta))
        delta.setdefault("side", str(spec.side))
        delta.setdefault("quantity", int(spec.quantity))
        delta.setdefault("lot_size", int(spec.lot_size))
        quantity = abs(int(spec.quantity))
        side = str(spec.side)
        release_evidence: Dict[str, Any] = {}
        if released_by:
            release_evidence["released_by"] = str(released_by)
        if extra_evidence:
            release_evidence["exposure_pin_proof"] = dict(extra_evidence)
        if str(getattr(spec, "release_rule", "")) == RULE_MIS_SQUAREOFF:
            quantity, side, release_evidence = self._mis_squareoff_size(
                plan, spec, authority=authority, binding=binding, evidence=release_evidence
            )
            if quantity == 0:
                stored = self.submissions.record_outcome(
                    plan_id=plan_id,
                    step_no=step_no,
                    state="no_op",
                    detail={
                        **release_evidence,
                        "delta": delta,
                        "note": "the attributed book is already flat; nothing to square off",
                    },
                )
                return dict(stored or {})
        elif str(getattr(spec, "release_rule", "")) == RULE_ROLL_CLOSE_RELEASED:
            # The roll's old-contract close is an ABSOLUTE FLAT for this
            # strategy's OWN attributed book. Sizing it from the frozen leg's
            # signed quantity would double it for a long-old roll, and sizing it
            # from anything but the book would let one strategy's close reach
            # another's shares.
            quantity, side, release_evidence = self._roll_close_size(
                plan, spec, authority=authority, binding=binding, evidence=release_evidence
            )
            if quantity == 0:
                stored = self.submissions.record_outcome(
                    plan_id=plan_id,
                    step_no=step_no,
                    state="no_op",
                    detail={
                        **release_evidence,
                        "delta": delta,
                        "note": "the attributed old-contract book is already flat",
                    },
                )
                return dict(stored or {})

        if self.intent_handler is None:
            raise LiveRefusal(
                "LIVE_INTENT_HANDLER_MISSING", {"plan_id": plan_id, "step_ref": step_ref}
            )

        # A GATED dependent leg is a bounded platform-side LIMIT, never a MARKET
        # order: the price is derived here, at release time, inside the band
        # frozen with the plan, and a price outside that band is a named refusal
        # rather than a widened bound. Non-gated legs keep their current
        # behaviour.
        execution_order = self._gated_execution_order(
            plan,
            spec,
            leg=leg,
            side=side,
            quantity=quantity,
            quote=quote,
            session_id=session_id,
        )

        from backend.algo_runtime.models import OrderIntent

        payload = {
            "session_id": session_id,
            "correlation_id": step_ref,
            "idempotency_key": step_ref,
            "order": {
                "exchange": str(spec.broker_exchange or spec.exchange or ""),
                "tradingsymbol": str(spec.broker_symbol or spec.tradingsymbol or ""),
                "transaction_type": side,
                "variety": str(spec.variety or "regular"),
                "product": str(spec.product or ""),
                "order_type": (
                    str(execution_order["order_type"]) if execution_order else ORDER_TYPE_MARKET
                ),
                "quantity": int(quantity),
                **({"price": float(execution_order["price"])} if execution_order else {}),
                "autoslice": should_autoslice(
                    str(spec.broker_exchange or spec.exchange or "")
                ),
                # Attribution binds the broker order id to THIS plan step's run
                # BEFORE the order exists, so ingestion can attribute the fill
                # without the child ever asserting ownership.
                "attribution": {
                    "strategy_run_id": str(binding.get("strategy_run_id") or ""),
                    "strategy_family": str(plan.get("plan_kind") or "single_instrument"),
                    "strategy_name": str(
                        plan.get("strategy_name") or plan.get("strategy_id") or ""
                    ),
                    "execution_mode": "live",
                    "account_ref": account_id,
                    "entry_surface": "hosted_plan",
                    "idempotency_key": step_ref,
                    "metadata": {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "strategy_id": strategy_id,
                    },
                },
            },
        }
        intent = OrderIntent(intent_type="place_order", payload=payload, dedupe_key=step_ref)
        if execution_order is not None:
            # PRE-SEND evidence: the frozen reference, the observed quote, the
            # chosen price, the band and the tick are durable BEFORE the broker
            # call, so a crash between here and the response still says exactly
            # what was about to be sent - and the timeout can measure how long
            # this order has worked from ``submitted_at``.
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="releasing",
                detail={"execution_order": execution_order, "delta": delta, **release_evidence},
            )
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
                    **release_evidence,
                    **({"execution_order": execution_order} if execution_order else {}),
                    "note": "work and reservation are retained; never auto-repeated",
                },
            )
            return dict(stored or {})

        order_ids = self._accepted_order_ids(result)
        if order_ids:
            stored = self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="pending",
                broker_order_ids=order_ids,
                detail={
                    "delta": delta,
                    **release_evidence,
                    **({"execution_order": execution_order} if execution_order else {}),
                    "note": "accepted: fills come from ingestion",
                },
            )
            return dict(stored or {})

        if self._is_explicit_rejection(result):
            # An AUTHORITATIVE refusal may resolve the known-unfilled residual.
            self.barrier.record_work_event(
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment="live",
                event="work_resolved",
                ref=step_ref,
                detail={"plan_id": plan_id, "outcome": "rejected"},
            )
            stored = self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="rejected",
                detail={
                    "delta": delta,
                    "result": result,
                    **release_evidence,
                    **({"execution_order": execution_order} if execution_order else {}),
                },
            )
            return dict(stored or {})

        # No order id and no authoritative refusal: the outcome is UNKNOWN. Work
        # and reservation stay held, and the step is never repeated.
        stored = self.submissions.record_outcome(
            plan_id=plan_id,
            step_no=step_no,
            state="uncertain",
            detail={
                "delta": delta,
                "result": result,
                **release_evidence,
                **({"execution_order": execution_order} if execution_order else {}),
                "note": "no authoritative order reference; recovery required",
            },
        )
        return dict(stored or {})

    # -- gated-LIMIT timeout ------------------------------------------------

    async def expire_timed_out_limits(
        self,
        *,
        now: Optional[datetime] = None,
        timeout_seconds: Optional[float] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Cancel GATED LIMIT orders that have worked past the platform timeout.

        Only an order id already KNOWN through the durable claim (the accepted
        response / ingestion) is cancelled, through the SAME broker intent
        boundary that placed it. The attempt is recorded durably BEFORE the cancel
        is sent, so a crash, a repeated pass or a restarted process can never send
        a second cancel; an UNCERTAIN cancel leaves the work unresolved for the
        existing fence/repair boundary instead of guessing.

        Outcomes are explicit and migration-free, in the states this protocol
        already owns:

        * zero-filled cancel -> terminal ``rejected`` carrying
          ``limit_timeout.terminal="cancelled"`` (an unfilled acquisition), or the
          named action-required ``repair_required`` when the leg was a RISK
          REDUCTION that could not be executed inside its bound;
        * partial -> ``partial`` with the remainder retained;
        * a cancel that raced a COMPLETE fill -> the claim is left for ingestion;
        * uncertain -> the claim stays in flight and is never re-cancelled.

        "Zero-filled" is measured from the platform's CONFIRMED ingestion at the
        instant of the cancel, which is the same evidence the ordinary outcome
        pass decides on; a trade that lands afterwards is still attributed to the
        run by ingestion.

        There is no automatic repricing and no replacement order.
        """
        counters: Dict[str, Any] = {
            "expired": 0,
            "partial": 0,
            "uncertain": 0,
            "skipped": 0,
            "filled": 0,
            "errors": 0,
        }
        declared: List[Dict[str, Any]] = []
        sweep_token = f"limit-timeout-{uuid.uuid4().hex}"
        lister = getattr(self.submissions, "working_limit_steps", None)
        if not callable(lister) or self.intent_handler is None:
            return {**counters, "declared": declared}
        try:
            rows = list(lister(limit=int(limit)) or [])
        except Exception as exc:  # noqa: BLE001 - an unreadable scan is not a clean one
            counters["errors"] = 1
            counters["error_detail"] = str(exc)
            return {**counters, "declared": declared}
        moment = now if now is not None else self._clock()
        timeout = (
            gated_limit_timeout_seconds() if timeout_seconds is None else float(timeout_seconds)
        )
        for row in rows:
            try:
                outcome = await self._expire_one_limit(
                    row,
                    moment=moment,
                    timeout=timeout,
                    sweep_token=sweep_token,
                )
            except Exception as exc:  # noqa: BLE001 - one bad row never kills the pass
                counters["errors"] += 1
                counters["error_detail"] = (
                    f"{row.get('plan_id')}:{row.get('step_no')}: {exc}"
                )
                continue
            if outcome is None:
                counters["skipped"] += 1
            elif outcome.get("declared"):
                counters["expired"] += 1
                declared.append(dict(outcome["declared"]))
            else:
                counters[str(outcome.get("tag") or "skipped")] += 1
        return {**counters, "declared": declared}

    async def _expire_one_limit(
        self,
        row: Mapping[str, Any],
        *,
        moment: datetime,
        timeout: float,
        sweep_token: str,
    ) -> Optional[Dict[str, Any]]:
        """Lease one claim, then hand its current row to the timeout decision."""
        plan_id = str(row.get("plan_id") or "")
        step_no = int(row.get("step_no") or 0)
        # Acquire BEFORE re-reading the row: the scan is only a work hint. The
        # consumer's live lease is never stolen, and a finalizing write belongs
        # to its holder even if that lease later expires.
        leased = self.submissions.acquire_lease(
            plan_id=plan_id,
            step_no=step_no,
            token=sweep_token,
            lease_seconds=self.sweep_lease_seconds,
        )
        if leased is None:
            return None
        try:
            return await self._expire_leased_limit(
                dict(leased),
                moment=moment,
                timeout=timeout,
                sweep_token=sweep_token,
            )
        finally:
            try:
                self.submissions.release_lease(
                    plan_id=plan_id, step_no=step_no, token=sweep_token
                )
            except Exception:  # noqa: BLE001 - an abandoned lease expires
                pass

    async def _expire_leased_limit(
        self,
        row: Mapping[str, Any],
        *,
        moment: datetime,
        timeout: float,
        sweep_token: str,
    ) -> Optional[Dict[str, Any]]:
        """One claim's timeout decision after the sweep owns its lease."""
        plan_id = str(row.get("plan_id") or "")
        step_no = int(row.get("step_no") or 0)
        state = str(row.get("state") or "")
        detail = dict(row.get("detail") or {})
        execution_order = dict(detail.get("execution_order") or {})
        if str(execution_order.get("order_type") or "") != ORDER_TYPE_LIMIT:
            return None
        # The consumer stages outcomes before its effects finish. The sweep has
        # no business regressing those writes, leased or not.
        if state in ("finalizing", "rejecting"):
            return None
        submitted_at = _as_datetime(execution_order.get("submitted_at"))
        if submitted_at is None:
            return None
        elapsed = (moment - submitted_at).total_seconds()
        if elapsed < timeout:
            return None
        prior = dict(detail.get("limit_timeout") or {})
        if prior.get("cancel_attempted_at") or prior.get("cancel_state") in (
            "attempted",
            "accepted",
            "uncertain",
            "refused",
        ):
            # The cancel is NEVER repeated - not by a later pass, and not by the
            # same pass after a restart.
            return None
        submitted_orders = [str(value) for value in (row.get("broker_order_ids") or []) if str(value)]
        if not submitted_orders:
            return None
        run_id = bound_run_for_plan(self.session_factory, plan_id=plan_id)
        child_orders = autoslice_child_order_ids(
            self.session_factory,
            account_id=str(row.get("account_id") or ""),
            run_id=str(run_id or ""),
            parent_order_ids=submitted_orders,
        )
        orders = merge_order_ids(submitted_orders, child_orders)
        delta = dict(row.get("delta_snapshot") or {})
        ordered = abs(int(delta.get("quantity") or execution_order.get("quantity") or 0))
        risk_reducing = not bool(delta.get("increases_exposure", True))
        attempt = {
            **prior,
            "cancel_attempted_at": moment.isoformat(),
            "cancel_state": "attempted",
            "order_ids": orders,
            "timeout_seconds": float(timeout),
            "elapsed_seconds": elapsed,
            "note": "platform timeout on a working gated LIMIT; no repricing, no replacement",
        }
        # PRE-SEND for the cancel: recorded before the broker call, so an
        # interrupted cancel can never be sent twice.
        self.submissions.record_outcome(
            plan_id=plan_id,
            step_no=step_no,
            state=state,
            detail={"execution_order": execution_order, "limit_timeout": attempt},
            consumer_token=sweep_token,
        )
        try:
            acknowledged, refused, results = await self._send_limit_cancel(
                plan_id=plan_id,
                step_ref=str(row.get("step_ref") or f"live-plan:{plan_id}:step:{step_no}"),
                orders=orders,
                session_id=str(execution_order.get("session_id") or ""),
                variety=str(execution_order.get("variety") or "regular"),
            )
        except Exception as exc:  # noqa: BLE001 - an uncertain cancel is not a retry
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state=state,
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **attempt,
                        "cancel_state": "uncertain",
                        "error": str(exc),
                        "note": (
                            "the cancel outcome is unknown: the work stays unresolved "
                            "for the fence/repair boundary and the cancel is never repeated"
                        ),
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "uncertain"}

        ingested_fills = self._confirmed_fill_total(plan_id=plan_id, orders=orders)
        broker_states = self._authoritative_order_states(results, orders=orders)
        resolved = {
            **attempt,
            "cancel_state": "accepted" if acknowledged else ("refused" if refused else "uncertain"),
            "cancel_results": list(results),
        }
        if ingested_fills is None or broker_states is None:
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state=state,
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **resolved,
                        "cancel_state": "uncertain",
                        "note": (
                            "broker boundary or confirmed-fill evidence was incomplete; "
                            "no terminal outcome is claimed"
                        ),
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "uncertain"}
        if not acknowledged:
            # An explicit broker refusal (or an ambiguous answer) is not an
            # outcome: the claim stays unresolved and is never re-cancelled.
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state=state,
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **resolved,
                        "note": (
                            "the broker did not acknowledge the cancel; the claim stays "
                            "unresolved and the cancel is never repeated"
                        ),
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "uncertain"}
        broker_fills = sum(
            int(state.get("filled_quantity") or 0) for state in broker_states.values()
        )
        all_broker_terminal = all(state.get("terminal") for state in broker_states.values())
        all_broker_refused = all(
            state.get("status") in ("CANCELLED", "REJECTED", "LAPSED")
            for state in broker_states.values()
        )
        fills = max(ingested_fills, broker_fills if all_broker_terminal else 0)
        resolved["broker_order_states"] = dict(broker_states)
        if ordered and fills >= ordered:
            # The cancel raced a COMPLETE fill: ingestion owns the finalisation.
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state=state,
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **resolved,
                        "filled_quantity": fills,
                        "note": "the order filled completely before the cancel landed; ingestion finalises it",
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "filled"}
        if fills > 0 and not (all_broker_terminal and all_broker_refused):
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state=state,
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **resolved,
                        "cancel_state": "uncertain",
                        "filled_quantity": fills,
                        "note": "broker state is terminal but ambiguous for a partial outcome",
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "uncertain"}
        if fills > 0:
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state="partial",
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **resolved,
                        "filled_quantity": fills,
                        "ordered_quantity": ordered,
                        "residual_quantity": max(0, ordered - fills),
                        "blocking": "limit_timeout_partial_residual_retained",
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "partial"}

        if not (all_broker_terminal and all_broker_refused):
            self.submissions.record_outcome(
                plan_id=plan_id,
                step_no=step_no,
                state=state,
                detail={
                    "execution_order": execution_order,
                    "limit_timeout": {
                        **resolved,
                        "cancel_state": "uncertain",
                        "filled_quantity": fills,
                        "note": "the broker order is not proven terminal refused; no zero-fill is claimed",
                    },
                },
                consumer_token=sweep_token,
            )
            return {"tag": "uncertain"}

        terminal_state = "repair_required" if risk_reducing else "rejected"
        self.submissions.record_outcome(
            plan_id=plan_id,
            step_no=step_no,
            state=terminal_state,
            detail={
                "execution_order": execution_order,
                "limit_timeout": {
                    **resolved,
                    "filled_quantity": 0,
                    "ordered_quantity": ordered,
                    "terminal": "cancelled",
                    "blocking": (
                        "limit_timeout_reduction_unfilled"
                        if risk_reducing
                        else "limit_timeout_unfilled"
                    ),
                    "action_required": bool(risk_reducing),
                },
            },
        )
        return {
            "tag": "expired",
            "declared": {
                "plan_id": plan_id,
                "step_no": step_no,
                "outcome": terminal_state,
                "filled": 0,
                "ordered": ordered,
            },
        }

    @staticmethod
    def _authoritative_order_states(
        results: Sequence[Any], *, orders: Sequence[str]
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Read terminal state/filled qty from the cancel boundary, or ``None``.

        A bare cancel acknowledgement is not proof: it carries no authoritative
        status or traded quantity. Evidence must cover EVERY known order id.
        """
        wanted = [str(value) for value in orders if str(value)]
        by_order: Dict[str, Dict[str, Any]] = {}
        terminal_statuses = {"COMPLETE", "CANCELLED", "CANCELED", "REJECTED", "LAPSED"}
        refused_statuses = {"CANCELLED", "CANCELED", "REJECTED", "LAPSED"}
        for result in results or []:
            payload = dict(result or {}) if isinstance(result, Mapping) else {}
            body = (
                payload.get("result")
                if isinstance(payload.get("result"), Mapping)
                else payload
            )
            if not isinstance(body, Mapping):
                continue
            order_id = str(
                body.get("order_id")
                or body.get("broker_order_id")
                or payload.get("order_id")
                or ""
            )
            if order_id not in wanted:
                continue
            status = str(body.get("status") or body.get("state") or "").upper()
            filled = body.get(
                "filled_quantity", body.get("filled_qty", body.get("traded_quantity"))
            )
            try:
                filled_quantity = max(0, int(filled)) if filled not in (None, "") else None
            except (TypeError, ValueError):
                return None
            if filled_quantity is None:
                return None
            by_order[order_id] = {
                "status": status,
                "terminal": status in terminal_statuses,
                "refused": status in refused_statuses,
                "filled_quantity": filled_quantity,
            }
        if not wanted or set(by_order) != set(wanted):
            return None
        return by_order

    async def _send_limit_cancel(
        self,
        *,
        plan_id: str,
        step_ref: str,
        orders: Sequence[str],
        session_id: str,
        variety: str,
    ) -> tuple[bool, bool, List[Any]]:
        """Cancel the step's KNOWN order ids through the broker intent boundary."""
        from backend.algo_runtime.models import OrderIntent

        acknowledged = False
        refused = False
        results: List[Any] = []
        for order_id in orders:
            dedupe_key = f"{step_ref}:cancel:{order_id}"
            intent = OrderIntent(
                intent_type="cancel_order",
                payload={
                    "session_id": session_id,
                    "correlation_id": f"{step_ref}:cancel",
                    "idempotency_key": dedupe_key,
                    "order": {"order_id": str(order_id), "variety": str(variety or "regular")},
                },
                dedupe_key=dedupe_key,
            )
            result = await self.intent_handler.handle(intent, context={"plan_id": plan_id})
            results.append(result)
            if self._cancel_acknowledged(result):
                acknowledged = True
            elif self._is_explicit_rejection(result):
                refused = True
        return acknowledged, refused, results

    @staticmethod
    def _cancel_acknowledged(result: Any) -> bool:
        payload = dict(result or {}) if isinstance(result, Mapping) else {}
        body = payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
        if not isinstance(body, Mapping):
            return False
        if body.get("order_id") or body.get("broker_order_id"):
            return True
        status = str(body.get("status") or body.get("state") or "").strip().lower()
        return status in (
            "cancelled",
            "canceled",
            "cancel_pending",
            "success",
            "ok",
            "submitted",
            "accepted",
        )

    def _confirmed_fill_total(self, *, plan_id: str, orders: Sequence[str]) -> Optional[int]:
        """Confirmed fill quantity for the step's orders, or ``None`` if unknown."""
        if self.fill_reader is None:
            return None
        try:
            rows = self.fill_reader(
                broker_order_ids=[str(value) for value in orders],
                plan={"plan_id": str(plan_id)},
            )
        except Exception:  # noqa: BLE001 - unknown fill evidence is UNKNOWN
            return None
        total = 0
        for row in rows or []:
            try:
                total += abs(int(dict(row).get("quantity") or 0))
            except (TypeError, ValueError):
                return None
        return total

    async def release_step(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        binding: Mapping[str, Any],
        authority: Mapping[str, Any],
        actor: str = "live-sequence",
        margin_evidence: Optional[Mapping[str, Any]] = None,
        catalog_state: Optional[Mapping[str, Any]] = None,
        session_id: Optional[str] = None,
        quote: Optional[Mapping[str, Any]] = None,
        quote_reader: Any = None,
        all_specs: Optional[Sequence[Any]] = None,
        parent: Optional[Mapping[str, Any]] = None,
        governed_authority_check: Optional[Callable[[Any], Optional[Mapping[str, Any]]]] = None,
        funds_reader: Optional[Callable[[], Any]] = None,
    ) -> Dict[str, Any]:
        """Release ONE ``withheld`` step, or refuse by name without placing anything.

        The whole release decision is durable and transactional: inside ONE
        transaction on the canonical book lock this re-reads every prerequisite's
        durable state, re-validates the exposure-increasing controls against the
        FRESH persisted authority and approval, re-checks that the reservation still
        covers every outstanding leg, and only then CASes ``withheld -> releasing``.
        A second pass (or a concurrent instance) therefore finds no ``withheld`` row
        and does nothing, and a crash after the CAS leaves an in-flight claim whose
        work and capacity stay held and which is NEVER re-sent.

        Dispatch happens AFTER the commit, exactly like the first-leg path, so the
        order can never exist without a durable claim that precedes it.
        """
        from .live_sequence import (
            LiveRefusal as _SequenceRefusal,
            RULE_STAGED_FUNDING_GATE,
            capacity_covers,
            prerequisites_met,
        )

        plan_id = str(plan.get("plan_id") or "")
        step_no = int(spec.step_no)
        account_id = str(plan.get("account_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        specs = list(all_specs or [spec])
        session = self.session_factory()
        now_expr = (
            "CURRENT_TIMESTAMP"
            if self.submissions._dialect(session) == "sqlite"
            else "NOW()"
        )
        try:
            self.barrier.lock_book(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment="live",
            )
            self._check_roll_peer_book(plan, release=True)
            # Phase 2: the governed authorization is re-derived INSIDE this
            # transaction, after the hosted-strategy row lock and therefore before
            # the ``withheld -> releasing`` CAS below. Revocation takes the same
            # lock, so either the revocation wins (nothing is released) or this
            # claim wins - which the audit records rather than pretending a stop
            # cancels an order the broker may already hold.
            if governed_authority_check is not None:
                self._lock_governed_strategy(session, strategy_id)
                governed_refusal = governed_authority_check(session)
                if governed_refusal is not None:
                    session.rollback()
                    raise LiveRefusal(
                        str(
                            governed_refusal.get("reason_code")
                            or "GOVERNED_RELEASE_REFUSED"
                        ),
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            **dict(governed_refusal),
                        },
                    )
            stored = self.submissions.get(plan_id=plan_id, step_no=step_no, db=session)
            if stored is None:
                raise LiveRefusal(
                    "LIVE_SUBMISSION_CLAIM_MISSING", {"plan_id": plan_id, "step_no": step_no}
                )
            if str(stored.get("state")) != "withheld":
                session.rollback()
                return {"state": str(stored.get("state")), "skipped": True, "released": False}

            states = self._step_states(session, plan_id)
            if not prerequisites_met(spec, states):
                session.rollback()
                raise LiveRefusal(
                    "LIVE_SEQUENCE_PREREQUISITE_UNFILLED",
                    {
                        "plan_id": plan_id,
                        "step_no": step_no,
                        "depends_on": [int(value) for value in (spec.depends_on or ())],
                        "states": {str(key): str(value) for key, value in states.items()},
                    },
                )
            # Every exposure-increasing control is re-read HERE, inside the release
            # transaction, against the FRESH persisted authority - never the
            # caller's copy from an earlier pass.
            self._check_authority(plan, authority, binding)
            # The approval is re-validated pin by pin. The exposure-snapshot pin is
            # the ONLY one a moving multi-leg book may legitimately change, and only
            # when this parent's own confirmed fills account for the movement
            # exactly; every other movement is a NAMED refusal so the operator
            # re-approves instead of trading an approved delta against a book it no
            # longer describes.
            approval, mismatched, pin_detail = self._approval_pin_state(plan)
            exposure_proof: Dict[str, Any] = {}
            if mismatched:
                unexplained = sorted(
                    set(mismatched) - set(SEQUENCE_TOLERATED_PIN_MISMATCHES)
                )
                # An option plan's approval carries a live reservation, and a
                # released/expired capacity is its OWN named blocker rather than a
                # generic approval failure: the owner is told to re-reserve, not
                # to re-approve a plan whose pins all still hold.
                if (
                    self._is_option_plan(plan)
                    and unexplained
                    and set(unexplained) <= {"RESERVATION_NOT_ACTIVE"}
                ):
                    self._check_reservation(plan)
                # A moved catalog GENERATION is stricter for options than for any
                # other lane: every option leg is a pinned derivative contract, so
                # the named option refusal is reported instead of the general
                # "relevant listing changed" pin.
                if (
                    self._is_option_plan(plan)
                    and unexplained
                    and set(unexplained) <= {"CATALOG_RELEVANT_CHANGE"}
                ):
                    self._check_approval_binding(plan, approval)
                if unexplained or parent is None:
                    raise LiveRefusal(
                        "LIVE_APPROVAL_INVALID",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            "approval_id": str(approval.get("approval_id") or ""),
                            "mismatched_pins": mismatched,
                            "explained_pins": sorted(
                                set(mismatched) & set(SEQUENCE_TOLERATED_PIN_MISMATCHES)
                            ),
                            "detail": pin_detail,
                            "message": (
                                "no durable parent is available to prove an exposure "
                                "snapshot change against"
                                if parent is None
                                else "an approval pin other than the exposure snapshot moved"
                            ),
                        },
                    )
                proved, exposure_proof = self._exposure_move_is_own_fills(
                    plan, specs, parent
                )
                if not proved:
                    raise LiveRefusal(
                        "LIVE_SEQUENCE_BOOK_MOVED_BEYOND_OWN_FILLS",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            "approval_id": str(approval.get("approval_id") or ""),
                            **exposure_proof,
                            "message": (
                                "the book moved by something other than this plan's own "
                                "confirmed fills, so the frozen delta no longer describes "
                                "it; re-approve the plan"
                            ),
                        },
                    )
            exposure_proof["pin"] = "EXPOSURE_SNAPSHOT_CHANGED"
            # S3: the version/policy and option-generation pins are re-checked in
            # the SAME release transaction, and an option plan's reservation is
            # re-read here (never renewed) so a released or expired capacity is a
            # named blocker rather than a silently released leg.
            self._check_approval_binding(plan, approval)
            if self._is_option_plan(plan):
                self._check_reservation(plan)
            staged_detail = None
            if str(getattr(spec, "release_rule", "")) == RULE_STAGED_FUNDING_GATE:
                # STAGE 1 - sequencing and price evidence only: it reads no money,
                # so it may run before the account lock.
                validated_quote = await self._staged_funding_quote_stage(
                    plan,
                    spec,
                    states=states,
                    quote=quote,
                    quote_reader=quote_reader,
                )
                staged_detail = {
                    "confirmed_reduction_steps": sorted(
                        int(value)
                        for value in spec.depends_on
                        if str(states.get(int(value)) or "") == "filled"
                    ),
                    "unconfirmed_reduction_steps": [],
                    "authorization_key": f"{plan_id}:{step_no}",
                }
                reservation = self._check_reservation(plan)
                covered, coverage = capacity_covers(reservation, specs)
                if not covered:
                    raise LiveRefusal(
                        "LIVE_CAPACITY_SHORTFALL",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            **coverage,
                            "message": "the reservation no longer covers every outstanding leg",
                        },
                    )
                # The reservation ACCOUNT lock, in the fixed order (canonical book
                # lock -> governed strategy lock -> account lock). The funds read
                # that decides whether this buy may spend happens INSIDE it - and
                # inside this same release transaction, together with the keyed
                # authorization and the ``withheld -> releasing`` CAS below - so a
                # competitor's authorization, spend or confirmed fill that lands
                # while we wait for the lock is reflected in the figure we decide
                # against. A pre-lock read is deliberately NOT the authority: it can
                # still contain cash another plan has already spent.
                self.ledger._lock_account(session, account_id)
                funds = self._staged_funding_funds_stage(
                    plan,
                    spec,
                    funds_reader=funds_reader,
                    margin_evidence=margin_evidence,
                    catalog_state=catalog_state,
                )
                try:
                    authorization = self.ledger.authorize_staged_increase(
                        plan_id=plan_id,
                        step_no=step_no,
                        requirement_inr=float(validated_quote["ltp"]) * abs(int(spec.quantity)),
                        account_capacity_inr=float(funds["usable"]),
                        quote=dict(validated_quote),
                        funds_evidence=dict(funds),
                        actor_id=actor,
                        db=session,
                    )
                except CapacityExceeded as exc:
                    raise LiveRefusal(
                        "ACCOUNT_FUNDS_UNSECURED",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            "authorization_key": f"{plan_id}:{step_no}",
                            "reservation_error": str(getattr(exc, "reason_code", type(exc).__name__)),
                            "reservation_error_detail": dict(getattr(exc, "detail", {}) or {}),
                        },
                    ) from exc
            else:
                reservation = self._check_reservation(plan)
                self._check_admission(
                    plan, margin_evidence=margin_evidence, catalog_state=catalog_state
                )
                covered, coverage = capacity_covers(reservation, specs)
                if not covered:
                    raise LiveRefusal(
                        "LIVE_CAPACITY_SHORTFALL",
                        {
                            "plan_id": plan_id,
                            "step_no": step_no,
                            **coverage,
                            "message": "the reservation no longer covers every outstanding leg",
                        },
                    )
            result = session.execute(
                text(
                    """
                    UPDATE public.live_plan_submissions
                    SET state = 'releasing', updated_at = {now}
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state = 'withheld'
                    """.format(now=now_expr)
                ),
                {"plan_id": plan_id, "step_no": step_no},
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                # Another instance released it between the read and the CAS.
                session.rollback()
                return {"state": "releasing", "skipped": True, "released": False}
            session.execute(
                text(
                    """
                    UPDATE public.live_plan_executions
                    SET state = 'executing', updated_at = {now}
                    WHERE plan_id = :plan_id AND state = 'planned'
                    """.format(now=now_expr)
                ),
                {"plan_id": plan_id},
            )
            session.commit()
        except LiveRefusal:
            session.rollback()
            raise
        except _SequenceRefusal:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        try:
            outcome = await self.dispatch_step(
                plan,
                spec,
                binding=binding,
                account_id=account_id,
                strategy_id=strategy_id,
                actor=actor,
                session_id=session_id,
                quote=quote,
                quote_reader=quote_reader,
                authority=authority,
                released_by="live-sequence",
                extra_evidence=(
                    {**exposure_proof, "staged_funding_gate": staged_detail}
                    if staged_detail
                    else (exposure_proof or None)
                ),
            )
        except LiveRefusal:
            # A refusal BEFORE the handler was called means nothing was sent. The
            # claim is rewound to ``withheld`` so a later pass can retry once the
            # evidence is available. A step that DID reach the handler cannot take
            # this path: transport uncertainty is recorded, not raised.
            self._rewind_release(plan_id=plan_id, step_no=step_no)
            raise
        return {"state": str(outcome.get("state") or ""), "skipped": False, "released": True}

    @staticmethod
    def _lock_governed_strategy(session: Any, strategy_id: str) -> None:
        """Take the SAME row lock a grant revocation takes (PostgreSQL only).

        SQLite serialises writes at the database level and does not support
        ``FOR UPDATE``, so this is a no-op there exactly as the canonical book
        lock is. A strategy with no hosted row (an external or legacy plan) has
        nothing to lock; the governed authority check that follows still runs.
        """
        dialect = getattr(getattr(session, "bind", None), "dialect", None)
        if getattr(dialect, "name", "") != "postgresql":
            return
        session.execute(
            text(
                "SELECT id FROM public.hosted_strategies WHERE id = :strategy_id "
                "FOR UPDATE"
            ),
            {"strategy_id": str(strategy_id)},
        ).fetchall()

    def _rewind_release(self, *, plan_id: str, step_no: int) -> None:
        session = self.session_factory()
        now_expr = (
            "CURRENT_TIMESTAMP"
            if self.submissions._dialect(session) == "sqlite"
            else "NOW()"
        )
        try:
            session.execute(
                text(
                    """
                    UPDATE public.live_plan_submissions
                    SET state = 'withheld', updated_at = {now}
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state = 'releasing'
                      AND (broker_order_ids IS NULL OR broker_order_ids = '[]'::jsonb)
                    """.format(now=now_expr)
                ),
                {"plan_id": str(plan_id), "step_no": int(step_no)},
            )
            session.commit()
        except Exception:  # noqa: BLE001 - the claim stays in flight, never lost
            session.rollback()
        finally:
            session.close()

    @staticmethod
    def _step_states(session: Any, plan_id: str) -> Dict[int, str]:
        rows = session.execute(
            text(
                "SELECT step_no, state FROM public.live_plan_submissions "
                "WHERE plan_id = :plan_id"
            ),
            {"plan_id": str(plan_id)},
        ).fetchall()
        return {int(row[0]): str(row[1] or "") for row in rows}

    def _mis_squareoff_size(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        authority: Optional[Mapping[str, Any]],
        binding: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> tuple[int, str, Dict[str, Any]]:
        """Size a MIS square-off from the CURRENT attributed book, never beyond it.

        The platform's own ``attributed_exit_size`` is the isolation guarantee: one
        strategy's square-off can never sell another strategy's shares. The frozen
        step delta is the REQUEST; the released quantity is the clamped one, and
        both travel in the trail so a partial exit is visible rather than inferred.
        """
        from .mis_squareoff import attributed_exit_size

        plan_id = str(plan.get("plan_id") or "")
        leg = self._leg_view(spec)
        try:
            current = int(self.position_reader(plan=dict(plan), leg=dict(leg)) or 0) if self.position_reader else 0
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "error": str(exc)},
            ) from exc
        requested = -abs(int(spec.quantity)) if current > 0 else abs(int(spec.quantity))
        try:
            signed = int(
                attributed_exit_size(
                    attributed_quantity=current, requested_quantity=requested
                )
            )
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "error": str(exc)},
            ) from exc
        quantity = abs(signed)
        side = "BUY" if signed > 0 else "SELL"
        detail = {
            **dict(evidence or {}),
            "mis_squareoff": True,
            "frozen_quantity": abs(int(spec.quantity)),
            "attributed_quantity": current,
            "released_quantity": int(quantity),
            "release_rule": str(getattr(spec, "release_rule", "")),
            "scheduled_at": str(
                (getattr(spec, "detail", {}) or {}).get("mis", {}).get("scheduled_at") or ""
            ),
            "authority_attempt": str((authority or {}).get("attempt") or ""),
            "strategy_run_id": str(binding.get("strategy_run_id") or ""),
        }
        return int(quantity), side, detail

    def _roll_close_size(
        self,
        plan: Mapping[str, Any],
        spec: Any,
        *,
        authority: Optional[Mapping[str, Any]],
        binding: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> tuple[int, str, Dict[str, Any]]:
        """Size a roll's old-contract close from the CURRENT attributed book.

        The platform's own ``attributed_exit_size`` is the isolation guarantee: an
        exit is clamped to what THIS strategy holds, so a roll's close can never
        reach another strategy's shares, and a request in the same direction as
        the book (which would GROW it) is zero rather than a trade. The frozen
        step's quantity is the REQUEST; the released quantity is the clamped one,
        and both travel in the trail so a partial close is visible rather than
        inferred.
        """
        from .mis_squareoff import attributed_exit_size

        plan_id = str(plan.get("plan_id") or "")
        leg = self._leg_view(spec)
        current = self._attributed_quantity(plan, dict(leg))
        roll_detail = dict((getattr(spec, "detail", {}) or {}).get("roll") or {})
        try:
            signed = int(
                attributed_exit_size(
                    attributed_quantity=current, requested_quantity=-int(current)
                )
            )
        except Exception as exc:  # noqa: BLE001 - unknown evidence is a refusal
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": plan_id, "error": str(exc)},
            ) from exc
        quantity = abs(signed)
        side = "BUY" if signed > 0 else "SELL"
        detail = {
            **dict(evidence or {}),
            "roll_close": True,
            "role": str(roll_detail.get("role") or ""),
            "roll_id": (
                None if roll_detail.get("roll_id") is None else str(roll_detail.get("roll_id"))
            ),
            "frozen_quantity": abs(int(getattr(spec, "quantity", 0) or 0)),
            "attributed_quantity": int(current),
            "released_quantity": int(quantity),
            "release_rule": str(getattr(spec, "release_rule", "")),
            "authority_attempt": str((authority or {}).get("attempt") or ""),
            "strategy_run_id": str(binding.get("strategy_run_id") or ""),
        }
        return int(quantity), side, detail

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

    #: State -> the trail event the plan's append-only audit records.
    _STEP_EVENT = {
        "pending": "submitted",
        "releasing": "submitted",
        "partial": "partially_filled",
        "finalizing": "partially_filled",
        "rejecting": "partially_filled",
        "repair_required": "partially_filled",
        "uncertain": "submitted",
        "rejected": "rejected",
        "no_op": "no_op",
        "filled": "filled",
        "residual_abandoned": "residual_abandoned",
        "withheld": "withheld",
    }

    _STEP_REASON = {
        "uncertain": "LIVE_TRANSPORT_UNCERTAIN",
        "rejected": "LIVE_ORDER_REJECTED",
    }

    def _step_entry(self, spec: Any, stored: Mapping[str, Any]) -> Dict[str, Any]:
        """One step's durable state as the executor/trail consume it."""
        state = str(stored.get("state") or "pending")
        snapshot = dict(stored.get("delta_snapshot") or {})
        detail = dict(stored.get("detail") or {})
        merged = {**snapshot, **detail}
        merged["delta"] = {
            "target": snapshot.get("target"),
            "current": snapshot.get("current"),
            "delta": snapshot.get("delta"),
            "side": snapshot.get("side"),
            "quantity": snapshot.get("quantity"),
            "lot_size": snapshot.get("lot_size"),
        }
        return {
            "step_no": int(getattr(spec, "step_no", 0) or 0),
            "step_ref": str(getattr(spec, "step_ref", "") or ""),
            "lane": str(getattr(spec, "lane", "") or ""),
            "event": self._STEP_EVENT.get(state, "submitted"),
            "state": state,
            "refusal_reason": self._STEP_REASON.get(state),
            "broker_order_ids": [str(value) for value in (stored.get("broker_order_ids") or [])],
            "depends_on": [int(value) for value in (getattr(spec, "depends_on", ()) or ())],
            "withheld": state == "withheld",
            "detail": merged,
        }

    def _submission_view(self, plan_id: str, specs: Sequence[Any]) -> LiveSubmission:
        """The plan's durable per-step state, with a primary step for Phase 1 callers.

        The primary step is the first step that is NOT withheld (the one this call
        could actually send); a plan whose every step is still withheld reports the
        first withheld step, so "nothing was submitted" is visible rather than
        looking like a silent success.
        """
        steps: List[Dict[str, Any]] = []
        primary: Optional[Dict[str, Any]] = None
        for spec in specs:
            stored = self.submissions.get(plan_id=plan_id, step_no=int(spec.step_no)) or {}
            entry = self._step_entry(spec, stored)
            steps.append(entry)
            if primary is None and entry["state"] != "withheld":
                primary = entry
        primary = primary or (steps[0] if steps else None)
        return LiveSubmission(
            plan_id=str(plan_id),
            state=str(primary["state"]) if primary else "",
            step_ref=str(primary["step_ref"]) if primary else "",
            broker_order_ids=list(primary["broker_order_ids"]) if primary else [],
            reason_code=(primary or {}).get("refusal_reason"),
            detail=dict(primary["detail"]) if primary else {},
            steps=steps,
        )

    def _as_submission(self, stored: Mapping[str, Any]) -> LiveSubmission:
        """One stored claim as a ``LiveSubmission`` (no parent protocol needed)."""
        state = str(stored.get("state") or "")
        return LiveSubmission(
            plan_id=str(stored.get("plan_id") or ""),
            state=state,
            step_ref=str(stored.get("step_ref") or ""),
            broker_order_ids=[str(value) for value in (stored.get("broker_order_ids") or [])],
            reason_code=self._STEP_REASON.get(state),
            detail={
                **dict(stored.get("delta_snapshot") or {}),
                **dict(stored.get("detail") or {}),
            },
            steps=[
                {
                    "step_no": int(stored.get("step_no") or 0),
                    "step_ref": str(stored.get("step_ref") or ""),
                    "event": self._STEP_EVENT.get(state, "submitted"),
                    "state": state,
                    "refusal_reason": self._STEP_REASON.get(state),
                    "broker_order_ids": [
                        str(value) for value in (stored.get("broker_order_ids") or [])
                    ],
                    "detail": {
                        **dict(stored.get("delta_snapshot") or {}),
                        **dict(stored.get("detail") or {}),
                    },
                }
            ],
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
