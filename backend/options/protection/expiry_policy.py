"""Expiry policy and evidence-gated settlement (D-7, D-8).

Two rules, both about not inferring things.

**The expiry policy is frozen with the structure.** A short leg cannot be
discovered on the last session to have been physical all along; by then the platform
either has the capability to take delivery or it does not. So the policy is chosen at
plan time and enforced from config, never hardcoded.

**Settlement is a claim about what happened at the exchange, and a claim needs
evidence.** Expiry time passing adjusts nothing: the position may still exist and
simply not be visible yet, and an adjustment booked against a position that is still
open is a phantom. Only an authoritative source — a broker ledger, a contract note,
an exchange file — moves the book, and the evidence is recorded append-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from backend.strategies.attribution_models import OptionSettlementEvidence

#: Days before expiry at which an unrolled structure warns its owner.
EXPIRY_WARNING_ENV = "OPTIONS_EXPIRY_WARNING_DAYS"
DEFAULT_EXPIRY_WARNING_DAYS = 5

#: Sources whose evidence is authoritative. A position disappearing is not one.
AUTHORITATIVE_SOURCES = ("broker_ledger", "contract_note", "exchange_file")

SETTLEMENT_KINDS = ("cash", "physical")

#: The policies a structure can be frozen with.
EXPIRY_POLICIES = (
    "exit_before_cutoff",
    "allow_cash_settlement",
    "allow_physical_settlement",
)


def expiry_warning_days() -> int:
    raw = os.environ.get(EXPIRY_WARNING_ENV)
    if raw is None:
        return DEFAULT_EXPIRY_WARNING_DAYS
    try:
        days = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_EXPIRY_WARNING_DAYS
    return days if days >= 0 else DEFAULT_EXPIRY_WARNING_DAYS


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def days_to_expiry(expiry: Any, *, now: datetime) -> Optional[int]:
    parsed = _as_date(expiry)
    if parsed is None:
        return None
    return (parsed - now.date()).days


class SettlementRefusal(Exception):
    reason_code = "SETTLEMENT_EVIDENCE_REQUIRED"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


@dataclass(frozen=True)
class ExpiryCheck:
    """What the cutoff check found. It reports; it never closes anything."""

    escalated: bool
    reason: str
    days_to_expiry: Optional[int] = None
    warning_days: int = DEFAULT_EXPIRY_WARNING_DAYS
    policy: str = "exit_before_cutoff"
    action_required: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "escalated": self.escalated,
            "reason": self.reason,
            "days_to_expiry": self.days_to_expiry,
            "warning_days": self.warning_days,
            "policy": self.policy,
            "action_required": self.action_required,
        }


class OptionExpiryPolicy:
    """Cutoff warnings and MIS square-off, without ever improvising a close."""

    def __init__(self, *, notifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None) -> None:
        self._notifier = notifier
        self.notified: List[tuple] = []

    def check(
        self,
        *,
        account_id: str,
        run: Mapping[str, Any],
        expiry: Any,
        product: str = "NRML",
        now: Optional[datetime] = None,
        notify: bool = True,
    ) -> ExpiryCheck:
        """Warn, escalate, or note that a MIS structure squares off instead.

        A MIS option structure never reaches expiry: it is intraday, and the platform
        square-off schedule (Phase 8) owns its ending. Reporting a cutoff warning for
        one would be a false alarm about a deadline that does not apply.
        """
        moment = now or datetime.now(timezone.utc)
        policy = str(run.get("expiry_policy") or "exit_before_cutoff")

        if str(product or "").upper() == "MIS":
            return ExpiryCheck(
                escalated=False,
                reason="mis_squared_off_by_schedule",
                policy=policy,
                action_required=False,
            )

        remaining = days_to_expiry(expiry, now=moment)
        window = expiry_warning_days()
        if remaining is None:
            return ExpiryCheck(
                escalated=False, reason="expiry_unavailable", warning_days=window, policy=policy
            )
        if remaining > window:
            return ExpiryCheck(
                escalated=False, reason="outside_window", days_to_expiry=remaining,
                warning_days=window, policy=policy,
            )

        if notify and self._notifier is not None:
            try:
                sent = bool(
                    self._notifier(
                        str(account_id),
                        {
                            "reason": "expiry_cutoff",
                            "days_to_expiry": remaining,
                            "expiry": str(expiry),
                            "policy": policy,
                        },
                    )
                )
            except Exception:  # noqa: BLE001 - the escalation must land regardless
                sent = False
            self.notified.append((account_id, remaining, sent))

        # Short liabilities inside the window need an operator: the platform does not
        # get to decide what the position was for.
        return ExpiryCheck(
            escalated=True,
            reason="expiry_cutoff",
            days_to_expiry=remaining,
            warning_days=window,
            policy=policy,
            action_required=policy == "exit_before_cutoff",
        )


class OptionSettlementService:
    """Records authoritative settlement evidence and applies the adjustment."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    def record_evidence(
        self,
        *,
        account_id: str,
        option_run_id: str,
        structure_digest: str,
        settlement_kind: str,
        evidence_source: str,
        evidence_ref: Mapping[str, Any],
        recorded_by: str,
        adjustment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record one piece of authoritative evidence.

        Refuses a source that is not authoritative. "The position disappeared" is the
        most tempting non-source there is, and it is exactly the one that books an
        adjustment against a position that may still be open.
        """
        if str(settlement_kind) not in SETTLEMENT_KINDS:
            raise SettlementRefusal({"settlement_kind": str(settlement_kind)})
        if str(evidence_source) not in AUTHORITATIVE_SOURCES:
            raise SettlementRefusal(
                {
                    "evidence_source": str(evidence_source),
                    "authoritative": list(AUTHORITATIVE_SOURCES),
                    "message": (
                        "Settlement requires an authoritative source; a position that "
                        "is merely not visible is not evidence that it was settled"
                    ),
                }
            )

        import uuid

        row = OptionSettlementEvidence(
            id=str(uuid.uuid4()),
            account_id=str(account_id),
            option_run_id=str(option_run_id),
            structure_digest=str(structure_digest),
            settlement_kind=str(settlement_kind),
            evidence_source=str(evidence_source),
            evidence_ref=dict(evidence_ref or {}),
            recorded_by=str(recorded_by),
            adjustment_id=adjustment_id,
        )
        with self.session_factory() as session:
            session.add(row)
            session.flush()
            view = self._view(row)
            session.commit()
        return view

    def settle(
        self,
        *,
        account_id: str,
        option_run_id: str,
        structure_digest: str,
        settlement_kind: str = "cash",
        evidence_source: Optional[str] = None,
        evidence_ref: Optional[Mapping[str, Any]] = None,
        recorded_by: str = "",
        adjustment_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Settle a structure — ONLY with evidence.

        With no evidence this records nothing and refuses. The negative case is the
        important one: expiry time passing must not move the book.
        """
        _ = now
        if not evidence_source:
            raise SettlementRefusal(
                {
                    "option_run_id": option_run_id,
                    "message": (
                        "Settlement requires recorded evidence from an authoritative "
                        "source; expiry time alone adjusts nothing"
                    ),
                }
            )
        evidence = self.record_evidence(
            account_id=account_id,
            option_run_id=option_run_id,
            structure_digest=structure_digest,
            settlement_kind=settlement_kind,
            evidence_source=evidence_source,
            evidence_ref=evidence_ref or {},
            recorded_by=recorded_by,
            adjustment_id=adjustment_id,
        )
        return {"settled": True, "run_state": "settled", "evidence": evidence}

    def evidence_for(self, *, option_run_id: str) -> List[Dict[str, Any]]:
        from sqlalchemy import select

        with self.session_factory() as session:
            rows = session.execute(
                select(OptionSettlementEvidence)
                .where(OptionSettlementEvidence.option_run_id == str(option_run_id))
                .order_by(OptionSettlementEvidence.created_at)
            ).scalars().all()
            return [self._view(row) for row in rows]

    @staticmethod
    def _view(row: OptionSettlementEvidence) -> Dict[str, Any]:
        return {
            "id": str(row.id),
            "account_id": str(row.account_id),
            "option_run_id": str(row.option_run_id),
            "structure_digest": str(row.structure_digest),
            "settlement_kind": str(row.settlement_kind),
            "evidence_source": str(row.evidence_source),
            "evidence_ref": dict(row.evidence_ref or {}),
            "recorded_by": str(row.recorded_by),
            "adjustment_id": str(row.adjustment_id) if row.adjustment_id else None,
        }


def option_settlement_axes(
    *, account_id: str, strategy_id: str, execution_environment: str, db: Any = None
) -> List[Dict[str, Any]]:
    """A Phase 5 domain adapter: what the option domain contributes to settlement.

    Registered rather than discovered, so the barrier never learns about option
    runs: it asks each domain what it knows and rolls the answers up. Two things it
    reports, and the distinction matters.

    A run that has recorded authoritative settlement evidence contributes ``settled``
    — the option domain is DONE with it. A run that is merely past its expiry
    contributes ``unsettled`` with a reason, because expiry time is not evidence and
    a barrier that treated it as one would release attribution against a position
    that may still exist.
    """
    axes: List[Dict[str, Any]] = []
    if db is None:
        return axes

    from sqlalchemy import select

    try:
        rows = db.execute(
            select(OptionSettlementEvidence).where(
                OptionSettlementEvidence.account_id == str(account_id)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 - an unavailable evidence table contributes nothing
        return axes

    settled_runs = {
        str(row.option_run_id)
        for row in rows
        if row.option_run_id is not None
    }
    if not settled_runs:
        # No evidence for anything: the domain has nothing to settle, which is a
        # contribution of its own — silence would read as "not asked".
        axes.append(
            {
                "name": "domain:option_settlement",
                "state": "unsettled",
                "detail": {"reason": "no_authoritative_settlement_evidence"},
            }
        )
        return axes

    axes.append(
        {
            "name": "domain:option_settlement",
            "state": "settled",
            "detail": {"option_runs": sorted(settled_runs)},
        }
    )
    return axes


def register_option_settlement_adapter() -> None:
    """Register the option domain adapter with the Phase 5 barrier.

    Idempotent: a module can be imported many times in a process (tests, reloads),
    and a registry that accumulated duplicates would report the same domain's axes
    more than once.
    """
    from backend.strategies.settlement import (
        register_domain_adapter,
        settlement_domain_adapters,
    )

    if option_settlement_axes in settlement_domain_adapters:
        return
    register_domain_adapter(option_settlement_axes)
