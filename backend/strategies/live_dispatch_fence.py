"""The durable PRE-SEND fence for one hosted live plan step.

A ``releasing`` claim means the release pass committed (``withheld ->
releasing``) and the process may or may not have reached the broker. "The claim
carries no broker order reference" is NOT proof that no order exists: the broker
can accept an order and the response can be lost (a socket close, a timeout, a
process death) before the reference is ever persisted. Abandoning on that
assumption would let a REAL position appear while the barrier reports the work
resolved - the exact quiet proof this platform refuses to produce.

Non-submission has to be PROVEN, and the platform already records the fact that
makes that possible. The live order path (``OrdersService.place_order`` with a
session) writes a durable ``live_order_intents`` row - keyed by the account and
the immutable ``idempotency_key`` the hosted adapter sets to the plan STEP
reference - BEFORE it calls the broker, and only then marks it with the broker's
order id. So, for one step:

* **no fence row** - every broker write on this path is preceded by that row, so
  its absence proves the send was never attempted. Non-submission is proven from
  platform data, and a bounded disposition may proceed.
* **a fence row naming a broker order** - the order EXISTS. That is not a reason
  to abandon anything: the reference is adopted onto the claim so ordinary
  ingestion owns it.
* **a fence row with no broker order id** - the send was ATTEMPTED and its
  outcome is UNKNOWN. Nothing may be abandoned, released or unblocked. The only
  thing that can resolve it is an authoritative broker read by the immutable
  client correlation: an order proves acceptance (adopt it); a COMPLETE read that
  does not contain it proves non-submission (the disposition may proceed); an
  unavailable or incomplete read stays unknown.

An incomplete or unavailable reading is always ``unknown``, never "absent":
"cannot tell" is not "cannot exist".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy import text

from backend.app.database import SessionLocal

#: The fence states. ``unknown`` is a real answer and never authorises anything.
FENCE_NOT_ATTEMPTED = "not_attempted"
FENCE_ORDER_KNOWN = "order_known"
FENCE_UNKNOWN = "unknown"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveDispatchFence:
    """Hash one hosted live step to the platform's durable pre-send records."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        broker_lookup: Optional[Callable[..., Mapping[str, Any]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        #: An optional AUTHORITATIVE broker read, called as
        #: ``broker_lookup(account_id=..., client_order_ref=..., idempotency_key=...)``
        #: and answering ``{"state": "present"|"absent"|"unknown", "order_id": ...}``.
        #: Absent (or answering anything else) the fence stays UNKNOWN, because a
        #: refusal to guess is the whole point of this module.
        self._broker_lookup = broker_lookup
        self._clock = clock or _utcnow

    # -- reads --------------------------------------------------------------

    def _rows(self, *, account_id: str, step_ref: str) -> list[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        """
                        SELECT intent_id, client_order_ref, account_id, strategy_run_id,
                               idempotency_key, broker_order_id, status, created_at
                        FROM public.live_order_intents
                        WHERE account_id = :account_id
                          AND idempotency_key = :step_ref
                        ORDER BY created_at
                        """
                    ),
                    {"account_id": str(account_id), "step_ref": str(step_ref)},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def prove(
        self,
        *,
        account_id: str,
        step_ref: str,
        plan_id: str = "",
        step_no: int = 0,
    ) -> Dict[str, Any]:
        """Classify one step's send: not attempted, order known, or unknown."""
        base: Dict[str, Any] = {
            "plan_id": str(plan_id),
            "step_no": int(step_no),
            "step_ref": str(step_ref),
            "account_id": str(account_id),
            "evidence": "live_order_intents_pre_send_fence",
            "at": self._clock().isoformat(),
        }
        try:
            rows = self._rows(account_id=account_id, step_ref=step_ref)
        except Exception as exc:  # noqa: BLE001 - an unreadable fence is UNKNOWN
            return {**base, "state": FENCE_UNKNOWN, "reason": "fence_read_failed", "error": str(exc)}
        known_orders = sorted(
            {str(row.get("broker_order_id")) for row in rows if row.get("broker_order_id")}
        )
        if not rows:
            return {
                **base,
                "state": FENCE_NOT_ATTEMPTED,
                "reason": "NO_PRE_SEND_RECORD",
                "fence_rows": 0,
                "message": (
                    "the durable pre-send record every broker write on this path "
                    "creates does not exist for this step, so nothing was sent"
                ),
            }
        if known_orders:
            return {
                **base,
                "state": FENCE_ORDER_KNOWN,
                "reason": "BROKER_ORDER_RECORDED",
                "broker_order_ids": known_orders,
                "fence_rows": len(rows),
                "intent_ids": [str(row.get("intent_id")) for row in rows],
                "client_order_refs": sorted(
                    {str(row.get("client_order_ref")) for row in rows if row.get("client_order_ref")}
                ),
                "message": "the send reached the broker and its reference is durable",
            }
        # The send was ATTEMPTED and no reference was persisted. Only an
        # authoritative read can resolve it, and an unavailable or partial read
        # never can.
        lookup = self._broker_lookup
        if lookup is None:
            return {
                **base,
                "state": FENCE_UNKNOWN,
                "reason": "NO_AUTHORITATIVE_BROKER_READ",
                "fence_rows": len(rows),
                "intent_ids": [str(row.get("intent_id")) for row in rows],
                "message": (
                    "the send was attempted and may have been accepted; without an "
                    "authoritative broker read the outcome is unknown"
                ),
            }
        # Every pre-send row names the immutable correlation a broker read needs.
        # A row that does not is UNCOVERED: proving the OTHER rows absent says
        # nothing about it, so the whole question stays UNKNOWN.
        refs = [str(row.get("client_order_ref") or "") for row in rows]
        if any(not ref for ref in refs):
            return {
                **base,
                "state": FENCE_UNKNOWN,
                "reason": "CLIENT_ORDER_REF_MISSING",
                "fence_rows": len(rows),
                "intent_ids": [str(row.get("intent_id")) for row in rows],
                "message": (
                    "a pre-send record names no client correlation, so it cannot be "
                    "looked up and the send outcome cannot be disproved"
                ),
            }
        for row, ref in zip(rows, refs):
            try:
                answer = lookup(
                    account_id=str(account_id),
                    client_order_ref=ref,
                    idempotency_key=str(step_ref),
                )
            except Exception as exc:  # noqa: BLE001 - a failed read is UNKNOWN
                return {
                    **base,
                    "state": FENCE_UNKNOWN,
                    "reason": "BROKER_READ_FAILED",
                    "client_order_ref": ref,
                    "error": str(exc),
                }
            state = str((answer or {}).get("state") or "unknown")
            if state == "present":
                order_id = str((answer or {}).get("order_id") or "")
                if not order_id:
                    return {
                        **base,
                        "state": FENCE_UNKNOWN,
                        "reason": "BROKER_READ_INCOMPLETE",
                        "client_order_ref": ref,
                    }
                return {
                    **base,
                    "state": FENCE_ORDER_KNOWN,
                    "reason": "BROKER_ORDER_FOUND",
                    "broker_order_ids": [order_id],
                    "client_order_ref": ref,
                    "fence_rows": len(rows),
                }
            if state == "absent":
                # One row proven absent is not the answer: EVERY attempted row has
                # to be absent before non-submission is proven.
                continue
            return {
                **base,
                "state": FENCE_UNKNOWN,
                "reason": "BROKER_READ_INCONCLUSIVE",
                "client_order_ref": ref,
                "broker_state": state,
            }
        return {
            **base,
            "state": FENCE_NOT_ATTEMPTED,
            "reason": "BROKER_HAS_NO_SUCH_ORDER",
            "fence_rows": len(rows),
            "client_order_refs": sorted(
                {str(row.get("client_order_ref")) for row in rows if row.get("client_order_ref")}
            ),
            "message": (
                "the pre-send record exists but the authoritative broker read is "
                "COMPLETE and contains no order for it, so nothing was accepted"
            ),
        }
