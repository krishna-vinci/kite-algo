"""Corporate-action detection: detect, freeze, escalate — never rebase (G13).

R3 §18 is explicit that V1 does not rebase a book automatically, and the reason is
that getting it wrong is worse than not doing it: a split absorbed silently
rewrites the strategy's quantity and cost basis with nobody in the loop, and the
owner finds out only when the P&L is wrong. So the first delivery is
**detection + freeze + escalation**, and the freeze lifts only when a human says
what happened.

The detection rule is narrow on purpose. A broker quantity that disagrees with
attributed + manual trades is only *suspicious* when the disagreement looks like a
corporate action — a simple quantity ratio with no offsetting trades to explain
it. A disagreement that unattributed trades DO explain is an ordinary ingest gap,
and calling it a corporate action would train the operator to ignore the alarm.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    StrategyCorporateActionEvent,
    StrategyCorporateActionEventLog,
)

#: The named reason this detection carries into the divergence record.
DETECTION_REASON = "SUSPECTED_CORPORATE_ACTION"

ACTION_KINDS = ("suspected_split", "suspected_bonus", "suspected_merger", "unclassified")

#: Simple ratios a corporate action actually produces. A 1.5x disagreement is a
#: bonus (2 for 3); 2x, 3x, 5x, 10x are the common splits; below 1 is a reverse
#: split or a merger.
_SIMPLE_RATIOS = {
    2.0: "suspected_split",
    3.0: "suspected_split",
    4.0: "suspected_split",
    5.0: "suspected_split",
    10.0: "suspected_split",
    1.5: "suspected_bonus",
    1.25: "suspected_bonus",
    1.1: "suspected_bonus",
    0.5: "suspected_merger",
    0.25: "suspected_merger",
    0.2: "suspected_merger",
    0.1: "suspected_merger",
}

#: How close a ratio must be to a simple one to count as that action.
_RATIO_TOLERANCE = 0.02


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def classify_ratio(broker_quantity: int, expected_quantity: int) -> Optional[str]:
    """The action kind a quantity ratio suggests, or ``None`` if it suggests none.

    ``None`` is a real answer: an unexplained divergence that is not a clean ratio
    is not evidence of a corporate action, and inventing a kind for it would make
    the classification meaningless.
    """
    if expected_quantity == 0:
        return None
    ratio = float(broker_quantity) / float(expected_quantity)
    if ratio <= 0:
        return None
    for simple, kind in sorted(_SIMPLE_RATIOS.items()):
        if abs(ratio - simple) <= _RATIO_TOLERANCE * simple:
            return kind
    return None


class CorporateActionDetector:
    """Detects, records, freezes and escalates. Never adjusts anything itself."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        notifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        freeze: Optional[Callable[..., None]] = None,
    ) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._notifier = notifier
        self._freeze = freeze

    # -- detection ----------------------------------------------------------

    def detect(
        self,
        *,
        account_id: str,
        coordinate: Mapping[str, Any],
        broker_quantity: int,
        attributed_quantity: int,
        manual_quantity: int,
        offsetting_trade_quantity: int = 0,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Classify a quantity divergence, or return ``None`` when it is ordinary.

        ``offsetting_trade_quantity`` is the quantity of fills already ingested for
        this coordinate that have not yet been attributed. When they account for
        the whole gap, the divergence is an ingest lag and not a corporate action.
        """
        moment = now or _utcnow()
        expected = int(attributed_quantity) + int(manual_quantity)
        gap = int(broker_quantity) - expected
        if gap == 0:
            return None
        if int(offsetting_trade_quantity) and abs(gap) <= abs(int(offsetting_trade_quantity)):
            # The trades explain it: this is ingestion catching up, not a split.
            return None

        kind = classify_ratio(int(broker_quantity), expected) or "unclassified"
        event = self._record(
            account_id=str(account_id),
            coordinate=dict(coordinate),
            action_kind=kind,
            evidence={
                "reason": DETECTION_REASON,
                "broker_quantity": int(broker_quantity),
                "attributed_quantity": int(attributed_quantity),
                "manual_quantity": int(manual_quantity),
                "expected_quantity": expected,
                "gap": gap,
                "ratio": (float(broker_quantity) / expected) if expected else None,
            },
            now=moment,
        )
        frozen = self._freeze_coordinate(
            account_id=str(account_id),
            coordinate=dict(coordinate),
            broker_quantity=int(broker_quantity),
            attributed_quantity=int(attributed_quantity),
            manual_quantity=int(manual_quantity),
            reason=DETECTION_REASON,
        )
        if frozen:
            self._log(event["id"], "freeze_confirmed", detail={"reason": DETECTION_REASON}, at=moment)
        escalated = self._escalate(event, now=moment)
        return {**event, "frozen": frozen, "escalated": escalated}

    # -- freeze -------------------------------------------------------------

    def _freeze_coordinate(
        self,
        *,
        account_id: str,
        coordinate: Mapping[str, Any],
        broker_quantity: int,
        attributed_quantity: int,
        manual_quantity: int,
        reason: str,
    ) -> bool:
        """Freeze via the Phase 2 divergence machinery, not a parallel mechanism.

        The classification is recorded as ``unexplained`` because that is the class
        that freezes new exposure while leaving risk-reducing exits permitted — the
        exact behaviour a suspected split needs. The named reason travels on the
        corporate-action event.
        """
        if self._freeze is not None:
            self._freeze(
                account_id=account_id,
                coordinate=coordinate,
                divergence_class="unexplained",
                broker_quantity=broker_quantity,
                attributed_quantity=attributed_quantity,
                manual_quantity=manual_quantity,
                reason=reason,
            )
            return True
        try:
            from backend.strategies.account_truth import AccountTruthStore

            store = AccountTruthStore(session_factory=self.session_factory)
            store.upsert_reconciliation(
                account_id=str(account_id),
                coordinate=(
                    int(coordinate.get("instrument_token") or 0),
                    str(coordinate.get("exchange") or ""),
                    str(coordinate.get("tradingsymbol") or ""),
                    str(coordinate.get("product") or ""),
                ),
                divergence_class="unexplained",
                broker_quantity=int(broker_quantity),
                attributed_quantity=int(attributed_quantity),
                manual_quantity=int(manual_quantity),
                refresh_attempts=0,
                resolved=False,
            )
            return True
        except SQLAlchemyError:
            return False

    # -- escalation ---------------------------------------------------------

    def _escalate(self, event: Mapping[str, Any], *, now: datetime) -> bool:
        """Escalate once, to the account owner, through the outbox."""
        if self._notifier is None:
            return False
        try:
            sent = bool(self._notifier(str(event["account_id"]), dict(event)))
        except Exception:  # noqa: BLE001 - escalation must never break detection
            return False
        if not sent:
            return False
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyCorporateActionEvent).where(
                    StrategyCorporateActionEvent.id == str(event["id"])
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.status = "escalated"
            row.escalated_at = now
            session.commit()
        self._log(str(event["id"]), "escalated", detail={"reason": DETECTION_REASON}, at=now)
        return True

    # -- resolution ---------------------------------------------------------

    def resolve(
        self,
        event_id: str,
        *,
        adjustment_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Lift the freeze via an explicit owner-verified adjustment.

        There is no automatic path, and there is deliberately no method that
        resolves without naming the adjustment: the adjustment IS the evidence
        that a human decided what happened.
        """
        moment = now or _utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyCorporateActionEvent).where(
                    StrategyCorporateActionEvent.id == str(event_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("corporate action event not found")
            row.status = "resolved"
            row.resolved_at = moment
            row.resolved_adjustment_id = str(adjustment_id)
            session.commit()
            resolved = {"id": str(row.id), "status": str(row.status),
                        "resolved_adjustment_id": str(row.resolved_adjustment_id)}
        self._log(
            str(event_id),
            "resolved",
            actor_id=actor_id,
            detail={"adjustment_id": str(adjustment_id)},
            at=moment,
        )
        return resolved

    # -- reads --------------------------------------------------------------

    def events(self, *, account_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            query = select(StrategyCorporateActionEvent).order_by(
                StrategyCorporateActionEvent.detected_at.desc()
            )
            if account_id:
                query = query.where(StrategyCorporateActionEvent.account_id == str(account_id))
            rows = session.execute(query.limit(int(limit))).scalars().all()
            return [self._view(row) for row in rows]

    def log_for(self, event_id: str) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyCorporateActionEventLog)
                .where(StrategyCorporateActionEventLog.event_id == str(event_id))
                .order_by(
                    StrategyCorporateActionEventLog.created_at,
                    StrategyCorporateActionEventLog.event,
                )
            ).scalars().all()
            return [
                {
                    "event": str(row.event),
                    "actor_id": row.actor_id,
                    "detail": dict(row.detail or {}),
                }
                for row in rows
            ]

    # -- internals ----------------------------------------------------------

    def _record(
        self,
        *,
        account_id: str,
        coordinate: Mapping[str, Any],
        action_kind: str,
        evidence: Mapping[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        event_id = str(uuid.uuid4())
        with self.session_factory() as session:
            session.add(
                StrategyCorporateActionEvent(
                    id=event_id,
                    account_id=str(account_id),
                    instrument_token=int(coordinate.get("instrument_token") or 0),
                    exchange=str(coordinate.get("exchange") or ""),
                    tradingsymbol=str(coordinate.get("tradingsymbol") or ""),
                    product=str(coordinate.get("product") or ""),
                    action_kind=str(action_kind),
                    evidence=dict(evidence or {}),
                    status="detected",
                    detected_at=now,
                )
            )
            session.commit()
        self._log(event_id, "detected", detail=dict(evidence or {}), at=now)
        return {
            "id": event_id,
            "account_id": str(account_id),
            "coordinate": dict(coordinate),
            "action_kind": str(action_kind),
            "status": "detected",
            "reason": DETECTION_REASON,
            "evidence": dict(evidence or {}),
        }

    def _log(
        self,
        event_id: str,
        event: str,
        *,
        actor_id: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
        at: Optional[datetime] = None,
    ) -> None:
        try:
            with self.session_factory() as session:
                # Strictly increasing per event: created_at is the only ordering the
                # log carries, and a whole detection can land on one instant.
                message = at or _utcnow()
                latest = session.execute(
                    select(func.max(StrategyCorporateActionEventLog.created_at)).where(
                        StrategyCorporateActionEventLog.event_id == str(event_id)
                    )
                ).scalar()
                stamp = message
                if isinstance(latest, datetime):
                    latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
                    if latest_dt >= stamp:
                        stamp = latest_dt + timedelta(microseconds=1)
                session.add(
                    StrategyCorporateActionEventLog(
                        id=str(uuid.uuid4()),
                        event_id=str(event_id),
                        event=str(event),
                        actor_id=actor_id,
                        detail=dict(detail or {}),
                        created_at=stamp,
                    )
                )
                session.commit()
        except SQLAlchemyError:
            pass

    @staticmethod
    def _view(row: StrategyCorporateActionEvent) -> Dict[str, Any]:
        return {
            "id": str(row.id),
            "account_id": str(row.account_id),
            "instrument_token": int(row.instrument_token),
            "exchange": str(row.exchange),
            "tradingsymbol": str(row.tradingsymbol),
            "product": str(row.product),
            "action_kind": str(row.action_kind),
            "status": str(row.status),
            "evidence": dict(row.evidence or {}),
            "resolved_adjustment_id": (
                str(row.resolved_adjustment_id) if row.resolved_adjustment_id else None
            ),
        }
