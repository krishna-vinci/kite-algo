"""Owner approval records and structural validity (G6).

Approval is the account owner authorising ONE immutable plan, and it is only
meaningful because it is bound to everything that must still hold when execution
begins (R3 §6 D2). Two consequences shape this module:

* **An approval that outlives its inputs is worse than no approval.** Structural
  validity compares every structural pin and reports *all* the ones that failed,
  so an operator sees the whole picture rather than the first problem. The one
  thing an unrelated catalog change must NOT do is invalidate the approval — a
  plan stays approved when an unrelated listing moves.

* **Expiry means no action, never late execution.** ``APPROVAL_EXPIRED`` is a
  validity failure like any other; nothing here executes anything, and this
  phase deliberately has no execution loop at all.

At most one ``active`` approval per plan exists, enforced by a partial unique
index in the database rather than by a read-then-write in Python, so a concurrent
double-approval resolves to one winner. Approvals are never rewritten: a
re-approval inserts a new row and marks the old one ``superseded``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import (
    Strategy,
    StrategyApproval,
    StrategyPositionProjection,
    StrategyProjectionState,
)

#: Every pin a validity check can report. Listed in the order they are reported.
PIN_NAMES = (
    "PLAN_HASH_MISMATCH",
    "EXPOSURE_SNAPSHOT_CHANGED",
    "RECONCILIATION_VERSION_CHANGED",
    "CATALOG_RELEVANT_CHANGE",
    "RESERVATION_NOT_ACTIVE",
    "SESSION_PRODUCT_INVALID",
    "APPROVAL_EXPIRED",
)

#: A reservation in one of these still backs the approval.
ACTIVE_RESERVATION_STATUSES = ("active", "renewed")

DEFAULT_APPROVAL_VALIDITY_SECONDS = 900


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _text_or_none(value: Any) -> Optional[str]:
    """A trimmed string, or ``None`` when the binding carries no value.

    ``None`` is the "not pinned" state: a legacy approval or an unresolved chain
    binds nothing, and binding nothing must never read as binding the empty
    string (which would refuse every later check against a real value).
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_generation(value: Any) -> Optional[int]:
    """A run's held leg generation, defaulting to the first one when absent."""
    parsed = _int_or_none(value)
    if parsed is None:
        return 1
    return parsed if parsed >= 1 else 1


def _frozen_option_run_block(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """The plan's frozen ``resolved_plan.option_run`` block, or ``{}``.

    The block is the IMMUTABLE target the plan compiled, so two approvals racing
    on one generation agree on what they are claiming without reading the run.
    """
    resolved = plan.get("resolved_plan") or {}
    block = resolved.get("option_run") if isinstance(resolved, Mapping) else None
    return dict(block) if isinstance(block, Mapping) else {}


class ApprovalError(Exception):
    reason_code = "APPROVAL_ERROR"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


class ApprovalNotOwner(ApprovalError):
    """Only the strategy's account owner may approve. There are no delegates."""

    reason_code = "APPROVAL_ACTOR_NOT_OWNER"


class ApprovalConflict(ApprovalError):
    """An active approval already exists with the same pins."""

    reason_code = "APPROVAL_ALREADY_ACTIVE"


class OptionGenerationOwned(ApprovalError):
    """Another approval already owns this option run's generation.

    Only a plan that MOVES a generation (an adjust/roll) reserves one, so the
    conflict is exactly "two plans would both take the run from generation N".
    The partial unique index is the real contract; this is how the loser reads.
    """

    reason_code = "APPROVAL_OPTION_GENERATION_OWNED"


class ApprovalNotFound(ApprovalError):
    reason_code = "APPROVAL_NOT_FOUND"


class ApprovalInputError(ApprovalError):
    reason_code = "APPROVAL_INPUT_INVALID"


class ApprovalNotRequired(ApprovalError):
    """Paper and dry-run plans are exempt from approval (D-1)."""

    reason_code = "APPROVAL_NOT_REQUIRED"


class ReservationNotActive(ApprovalError):
    """Approval carries the reservation identity, so it cannot authorise work
    whose capacity was released or expired."""

    reason_code = "RESERVATION_NOT_ACTIVE"


@dataclass(frozen=True)
class ApprovalRequest:
    plan: Mapping[str, Any]
    actor_id: str
    reservation_id: str
    execution_environment: str = "live"
    validity_seconds: int = DEFAULT_APPROVAL_VALIDITY_SECONDS
    session_product_snapshot: Optional[Dict[str, Any]] = None
    margin_evidence: Optional[Dict[str, Any]] = None
    #: The immutable version identity the originating request pinned. Supplying
    #: it explicitly is authoritative - an unreadable plan chain must never be
    #: read as "no version to bind". Absent, the plan's own persisted chain is
    #: resolved instead, and an unreadable chain binds nothing.
    version_binding: Optional[Dict[str, Any]] = None
    #: The frozen option target/generation/policy, likewise authoritative when
    #: supplied by the caller that resolved it.
    option_binding: Optional[Dict[str, Any]] = None


class ApprovalService:
    """Creates, revokes and validates approvals. Never executes anything."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # -- reads --------------------------------------------------------------

    def owner_of(self, strategy_id: str) -> Optional[str]:
        with self.session_factory() as session:
            row = session.execute(
                select(Strategy.owner_id).where(Strategy.id == str(strategy_id))
            ).scalar_one_or_none()
            return str(row) if row is not None else None

    def exposure_snapshot(self, *, account_id: str, strategy_id: str) -> Dict[str, Any]:
        """The strategy's live-book snapshot an approval pins.

        A book that has never been published is snapshot ``version 0 / null`` —
        a real state, not a missing one, and it must stay distinguishable from a
        published empty book (version >= 1 with a content hash).
        """
        with self.session_factory() as session:
            state = session.execute(
                select(StrategyProjectionState).where(
                    StrategyProjectionState.account_id == str(account_id),
                    StrategyProjectionState.strategy_id == str(strategy_id),
                    StrategyProjectionState.execution_environment == "live",
                )
            ).scalar_one_or_none()
            if state is None:
                return {"projection_version": 0, "content_sha256": None, "published": False}
            return {
                "projection_version": int(state.projection_version or 0),
                "content_sha256": state.content_sha256,
                "published": True,
            }

    def reconciliation_version(self, *, account_id: str) -> int:
        from backend.strategies.admission import AdmissionService

        return AdmissionService(session_factory=self.session_factory).reconciliation_version(
            account_id=account_id
        )

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyApproval).where(StrategyApproval.approval_id == str(approval_id))
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def active_for_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyApproval).where(
                    StrategyApproval.plan_id == str(plan_id),
                    StrategyApproval.status == "active",
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def list_for_plan(self, plan_id: str) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyApproval)
                .where(StrategyApproval.plan_id == str(plan_id))
                .order_by(StrategyApproval.created_at.desc())
            ).scalars().all()
            return [self._view(row) for row in rows]

    def list_for_strategy(self, *, strategy_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyApproval)
                .where(StrategyApproval.strategy_id == str(strategy_id))
                .order_by(StrategyApproval.created_at.desc())
                .limit(int(limit))
            ).scalars().all()
            return [self._view(row) for row in rows]

    # -- approve / revoke ---------------------------------------------------

    def approve(
        self,
        request: ApprovalRequest,
        *,
        now: Optional[datetime] = None,
        actor_kind: str = "manual",
        evidence: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Bind an immutable plan and all its pins to one owner authorisation.

        ``actor_kind`` distinguishes the owner acting on one plan from the server
        recording a standing grant's authorisation. The authority check is the
        same either way - the owner issued the grant - but an automatic decision
        is never *readable* as a manual click, and its evidence travels with it.
        """
        kind = str(actor_kind or "manual")
        if kind not in ("manual", "automatic"):
            raise ApprovalInputError({"actor_kind": kind})
        moment = now or _utcnow()
        plan = dict(request.plan)
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id or not strategy_id:
            raise ApprovalInputError({"message": "plan_id and strategy_id are required"})

        if str(request.execution_environment).lower() != "live":
            # Approval authority expires with a live evaluation; paper and dry-run
            # are exempt, and recording an approval for them would be misleading.
            raise ApprovalNotRequired(
                {
                    "strategy_id": strategy_id,
                    "execution_environment": str(request.execution_environment),
                    "message": "Paper and dry-run plans are exempt from approval.",
                }
            )

        owner = self.owner_of(strategy_id)
        if owner is None:
            raise ApprovalInputError({"strategy_id": strategy_id})
        if str(request.actor_id) != owner:
            # Worker tokens and any other app user are refused: approval is the
            # account owner's act, and this release has no delegated roles.
            raise ApprovalNotOwner(
                {
                    "strategy_id": strategy_id,
                    "actor_id": str(request.actor_id),
                    "message": "Only the strategy's account owner may approve a plan.",
                }
            )

        # The reservation must exist and still be active: approval carries the
        # reservation identity, so it can never authorise work whose capacity was
        # released or expired.
        from backend.strategies.reservations import ReservationLedger

        ledger = ReservationLedger(session_factory=self.session_factory)
        reservation = ledger.get(request.reservation_id)
        if reservation is None:
            raise ApprovalInputError({"reservation_id": str(request.reservation_id)})
        if str(reservation["status"]) not in ACTIVE_RESERVATION_STATUSES:
            raise ReservationNotActive(
                {
                    "reservation_id": str(request.reservation_id),
                    "status": str(reservation["status"]),
                }
            )
        if str(reservation["plan_id"]) != plan_id:
            raise ApprovalInputError(
                {"message": "The reservation belongs to a different plan."}
            )

        snapshot = self.exposure_snapshot(account_id=account_id, strategy_id=strategy_id)
        catalogue = self._catalog_state(plan)
        # The version identity and the option target are resolved ONCE, before the
        # write, so the row records what the owner authorised rather than whatever
        # the stores say a moment later. An explicit binding from the caller (the
        # originating request) wins; otherwise the plan's own persisted chain is
        # asked, and an unresolved chain binds nothing.
        version = dict(request.version_binding or {}) or self.version_binding_for_plan(plan)
        option = dict(request.option_binding or {}) or self.option_binding_for_plan(plan)
        pins = {
            "plan_hash": str(plan.get("plan_hash") or ""),
            "exposure_snapshot_version": int(snapshot["projection_version"]),
            "exposure_snapshot_hash": snapshot["content_sha256"],
            "reconciliation_version": self.reconciliation_version(account_id=account_id),
            "catalog_generation": str(plan.get("pinned_catalog_generation") or ""),
            "strategy_version_id": _text_or_none(version.get("strategy_version_id")),
            "version_number": _int_or_none(version.get("version_number")),
            "source_sha256": _text_or_none(version.get("source_sha256")),
            "policy_hash": _text_or_none(version.get("policy_hash")),
            "option_run_id": _text_or_none(option.get("option_run_id")),
            "based_on_generation": _int_or_none(option.get("based_on_generation")),
            "protection_policy_version": _text_or_none(
                option.get("protection_policy_version")
            ),
            "reserved_option_generation": _int_or_none(
                option.get("reserved_option_generation")
            ),
        }
        catalogue_generation = str(catalogue.get("current_catalog_generation") or pins["catalog_generation"])
        session_product = dict(request.session_product_snapshot or {})

        session = self.session_factory()
        try:
            existing = session.execute(
                select(StrategyApproval).where(
                    StrategyApproval.plan_id == plan_id, StrategyApproval.status == "active"
                )
            ).scalar_one_or_none()
            if existing is not None:
                if self._pins_equal(existing, pins, session_product):
                    raise ApprovalConflict(
                        {
                            "approval_id": str(existing.approval_id),
                            "plan_id": plan_id,
                            "message": (
                                "This plan already has an active approval with identical pins; "
                                "re-approval would add nothing."
                            ),
                        }
                    )
                # Pins moved: supersede rather than rewrite. The old authorisation
                # remains queryable as part of the record.
                existing.status = "superseded"

            # A generation reservation is owned by exactly ONE active approval; the
            # partial unique index enforces it, and this read gives the loser a
            # named conflict in the uncontended case.
            if pins["option_run_id"] and pins["reserved_option_generation"] is not None:
                owner = session.execute(
                    select(StrategyApproval).where(
                        StrategyApproval.option_run_id == pins["option_run_id"],
                        StrategyApproval.reserved_option_generation
                        == pins["reserved_option_generation"],
                        StrategyApproval.status == "active",
                    )
                ).scalar_one_or_none()
                if owner is not None and str(owner.plan_id) != plan_id:
                    raise OptionGenerationOwned(
                        {
                            "plan_id": plan_id,
                            "option_run_id": pins["option_run_id"],
                            "reserved_option_generation": pins["reserved_option_generation"],
                            "owning_approval_id": str(owner.approval_id),
                            "owning_plan_id": str(owner.plan_id),
                            "message": (
                                "another plan's approval already owns this option run "
                                "generation: a run is moved from one generation by exactly "
                                "one approved plan"
                            ),
                        }
                    )

            approval_id = str(uuid.uuid4())
            row = StrategyApproval(
                approval_id=approval_id,
                plan_id=plan_id,
                strategy_id=strategy_id,
                account_id=account_id,
                reservation_id=str(request.reservation_id),
                plan_hash=pins["plan_hash"],
                exposure_snapshot_version=pins["exposure_snapshot_version"],
                exposure_snapshot_hash=pins["exposure_snapshot_hash"],
                reconciliation_version=pins["reconciliation_version"],
                catalog_generation=catalogue_generation,
                session_product_snapshot=session_product,
                strategy_version_id=pins["strategy_version_id"],
                version_number=pins["version_number"],
                source_sha256=pins["source_sha256"],
                policy_hash=pins["policy_hash"],
                option_run_id=pins["option_run_id"],
                based_on_generation=pins["based_on_generation"],
                protection_policy_version=pins["protection_policy_version"],
                reserved_option_generation=pins["reserved_option_generation"],
                actor_id=str(request.actor_id),
                actor_kind=kind,
                authorization_evidence=dict(evidence or {}),
                status="active",
                valid_from=moment,
                valid_until=moment + timedelta(seconds=int(request.validity_seconds)),
                created_at=moment,
            )
            session.add(row)
            session.commit()
            return self._view(row)
        except IntegrityError as exc:
            session.rollback()
            detail = str(getattr(exc, "orig", exc))
            if "uq_approvals_option_generation_active" in detail:
                # Lost the race for one option run generation: the winner's
                # approval is the one that may move the run.
                raise OptionGenerationOwned(
                    {
                        "plan_id": plan_id,
                        "option_run_id": pins["option_run_id"],
                        "reserved_option_generation": pins["reserved_option_generation"],
                        "message": "another approval won the race for this option run generation",
                    }
                ) from exc
            # Lost the race on uq_approvals_plan_active: the winner's approval is
            # the live one, and two active approvals are structurally impossible.
            raise ApprovalConflict(
                {"plan_id": plan_id, "message": "Another approval for this plan won the race."}
            ) from exc
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    def revoke(
        self, approval_id: str, *, actor_id: str, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Revoke an approval. Owner-only, and terminal."""
        moment = now or _utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyApproval).where(StrategyApproval.approval_id == str(approval_id))
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFound({"approval_id": str(approval_id)})
            owner = session.execute(
                select(Strategy.owner_id).where(Strategy.id == str(row.strategy_id))
            ).scalar_one_or_none()
            if owner is None or str(actor_id) != str(owner):
                raise ApprovalNotOwner(
                    {"approval_id": str(approval_id), "actor_id": str(actor_id)}
                )
            if str(row.status) != "active":
                raise ApprovalError(
                    {
                        "rejection_reason": "APPROVAL_NOT_ACTIVE",
                        "approval_id": str(approval_id),
                        "status": str(row.status),
                    }
                )
            row.status = "revoked"
            row.valid_until = moment
            session.commit()
            return self._view(row)

    # -- structural validity ------------------------------------------------

    def structural_validity(
        self,
        plan: Mapping[str, Any],
        approval: Mapping[str, Any],
        *,
        current: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Every pin that failed, not the first (D-6).

        ``current`` may be supplied to make the check a pure function of evidence;
        otherwise it is read from the stores. An unrelated catalog change leaves
        the approval valid, which is the rule that keeps a plan from being
        hostage to listings it does not touch.
        """
        moment = now or _utcnow()
        state = dict(current or {})
        if not state:
            state = self._current_state(plan, approval)

        mismatched: List[str] = []
        details: Dict[str, Any] = {}

        if str(state.get("plan_hash") or "") != str(approval.get("plan_hash") or ""):
            mismatched.append("PLAN_HASH_MISMATCH")
            details["plan_hash"] = {
                "approved": str(approval.get("plan_hash") or ""),
                "current": str(state.get("plan_hash") or ""),
            }

        if (
            int(state.get("exposure_snapshot_version") or 0)
            != int(approval.get("exposure_snapshot_version") or 0)
            or state.get("exposure_snapshot_hash") != approval.get("exposure_snapshot_hash")
        ):
            mismatched.append("EXPOSURE_SNAPSHOT_CHANGED")
            details["exposure_snapshot"] = {
                "approved_version": int(approval.get("exposure_snapshot_version") or 0),
                "current_version": int(state.get("exposure_snapshot_version") or 0),
            }

        if int(state.get("reconciliation_version") or 0) != int(
            approval.get("reconciliation_version") or 0
        ):
            mismatched.append("RECONCILIATION_VERSION_CHANGED")
            details["reconciliation_version"] = {
                "approved": int(approval.get("reconciliation_version") or 0),
                "current": int(state.get("reconciliation_version") or 0),
            }

        catalog_state = dict(state.get("catalog_state") or {})
        if str(catalog_state.get("state") or "valid") != "valid":
            mismatched.append("CATALOG_RELEVANT_CHANGE")
            details["catalog"] = catalog_state

        if str(state.get("reservation_status") or "") not in ACTIVE_RESERVATION_STATUSES:
            mismatched.append("RESERVATION_NOT_ACTIVE")
            details["reservation_status"] = str(state.get("reservation_status") or "")

        approved_products = sorted(
            str(item).upper()
            for item in (approval.get("session_product_snapshot") or {}).get("products", [])
        )
        current_products = state.get("products")
        if current_products is not None and sorted(
            str(item).upper() for item in current_products
        ) != approved_products:
            mismatched.append("SESSION_PRODUCT_INVALID")
            details["products"] = {
                "approved": approved_products,
                "current": sorted(str(item).upper() for item in current_products),
            }

        valid_until = _as_datetime(approval.get("valid_until"))
        if valid_until is not None and moment >= valid_until:
            mismatched.append("APPROVAL_EXPIRED")
            details["valid_until"] = valid_until.isoformat()

        return {
            "valid": not mismatched,
            "mismatched_pins": mismatched,
            "detail": details,
            "checked_at": moment.isoformat(),
        }

    def _current_state(self, plan: Mapping[str, Any], approval: Mapping[str, Any]) -> Dict[str, Any]:
        strategy_id = str(plan.get("strategy_id") or approval.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or approval.get("account_id") or "")
        snapshot = self.exposure_snapshot(account_id=account_id, strategy_id=strategy_id)
        reservation = None
        try:
            from backend.strategies.reservations import ReservationLedger

            reservation = ReservationLedger(session_factory=self.session_factory).get(
                str(approval.get("reservation_id") or "")
            )
        except Exception:  # noqa: BLE001 - an unreadable reservation is not "active"
            reservation = None
        return {
            "plan_hash": str(plan.get("plan_hash") or ""),
            "exposure_snapshot_version": int(snapshot["projection_version"]),
            "exposure_snapshot_hash": snapshot["content_sha256"],
            "reconciliation_version": self.reconciliation_version(account_id=account_id),
            "catalog_state": self._catalog_state(plan),
            "reservation_status": None if reservation is None else str(reservation["status"]),
            "products": self._plan_products(plan),
        }

    def _catalog_state(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            from backend.strategies.proposals import plan_invalidation_state

            return dict(plan_invalidation_state(dict(plan), session_factory=self.session_factory) or {})
        except Exception:  # noqa: BLE001 - unreadable catalog is not "valid"
            return {"state": "invalidated", "reason": "CATALOG_STATE_UNAVAILABLE"}

    @staticmethod
    def _plan_products(plan: Mapping[str, Any]) -> List[str]:
        products = [
            str(leg.get("product") or "").upper()
            for leg in (plan.get("resolved_plan") or {}).get("legs") or []
            if leg.get("product")
        ]
        return sorted(set(products))

    # -- version / option binding (C1.2 S3) ---------------------------------

    def version_binding_for_plan(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The version identity this plan can be TRACED to, or ``{}``.

        Resolution walks only PERSISTED authority - the plan's proposal, the
        hosted job that produced it and the immutable version that job pinned -
        so the binding can never be reinterpreted against a version the strategy
        adopted later. ``policy_hash`` comes from the plan's own durable
        ``HostedExecutionRequest`` when one exists, because that request is the
        thing that pinned the policy basis. An unreadable chain binds nothing
        (``{}``), which the release check treats as "nothing to compare" rather
        than as a match.
        """
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id:
            return {}
        from sqlalchemy import or_

        from backend.strategies.attribution_models import StrategyPlan, StrategyProposal
        from backend.strategies.models import (
            HostedStrategyVersion,
            StrategyJob,
        )

        binding: Dict[str, Any] = {}
        try:
            with self.session_factory() as session:
                row = session.execute(
                    select(
                        HostedStrategyVersion.id,
                        HostedStrategyVersion.version,
                        HostedStrategyVersion.source_sha256,
                    )
                    .join(
                        StrategyJob,
                        StrategyJob.version_id == HostedStrategyVersion.id,
                    )
                    .join(
                        StrategyProposal,
                        or_(
                            StrategyProposal.job_id == StrategyJob.id,
                            StrategyProposal.strategy_run_id == StrategyJob.run_id,
                        ),
                    )
                    .join(
                        StrategyPlan,
                        StrategyPlan.proposal_id == StrategyProposal.proposal_id,
                    )
                    .where(StrategyPlan.plan_id == plan_id)
                    .order_by(StrategyJob.created_at.desc())
                    .limit(1)
                ).first()
        except SQLAlchemyError:
            return {}
        if row is not None:
            binding["strategy_version_id"] = str(row[0])
            binding["version_number"] = int(row[1])
            binding["source_sha256"] = str(row[2])
        policy_hash = self.current_policy_hash(str(plan.get("strategy_id") or ""))
        if policy_hash:
            binding["policy_hash"] = policy_hash
        return binding

    def current_policy_hash(self, strategy_id: str) -> Optional[str]:
        """The strategy's CURRENT admission + protection policy hash, or ``None``.

        The hash is derived exactly as the execution-request service derives it,
        so an approval's pinned ``policy_hash`` is comparable at release: a policy
        that moved - including a tightening - refuses by name instead of trading
        under a basis the owner never saw. A strategy with no hosted row (or an
        unreadable policy) yields ``None``, which binds nothing.
        """
        strategy_id = str(strategy_id or "")
        if not strategy_id:
            return None
        from backend.strategies.execution_authorization import (
            ExecutionAuthorizationService,
        )
        from backend.strategies.models import HostedStrategy

        service = ExecutionAuthorizationService(session_factory=self.session_factory)
        try:
            with self.session_factory() as session:
                strategy = session.execute(
                    select(HostedStrategy).where(HostedStrategy.id == strategy_id)
                ).scalar_one_or_none()
                if strategy is None:
                    return None
                snapshot = service.policy_snapshot(session, strategy)
                return str(service.policy_hash_for(snapshot))
        except SQLAlchemyError:
            return None

    def option_binding_for_plan(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The frozen option target this plan binds, or ``{}`` for a non-option.

        ``option_run_id`` and ``based_on_generation`` are read from the IMMUTABLE
        frozen block, so two approvals racing on one generation agree on what
        they are claiming. ``reserved_option_generation`` is set only for a
        generation-MOVING plan (an adjust/roll); an exit closes the run rather
        than moving it, so several exits may reference one generation. The
        protection policy version is the owner row's, and an unreadable or
        absent owner leaves it unset - the release check then refuses by name
        rather than treating "unknown" as "no policy".
        """
        block = _frozen_option_run_block(plan)
        option_run_id = _text_or_none(block.get("option_run_id"))
        if not option_run_id:
            return {}
        phase = str(block.get("phase") or "").strip().lower()
        binding: Dict[str, Any] = {"option_run_id": option_run_id}
        if phase == "adjust":
            generation = _int_or_none(block.get("based_on_generation"))
            binding["based_on_generation"] = generation
            binding["reserved_option_generation"] = generation
        state = self._option_owner_state(option_run_id)
        if state.get("protection_policy_version"):
            binding["protection_policy_version"] = state["protection_policy_version"]
        return binding

    def option_binding_state(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The CURRENT option target facts a live release compares its pins to."""
        block = _frozen_option_run_block(plan)
        option_run_id = _text_or_none(block.get("option_run_id"))
        state: Dict[str, Any] = {
            "option_run_id": option_run_id,
            "phase": str(block.get("phase") or "").strip().lower(),
            "based_on_generation": _int_or_none(block.get("based_on_generation")),
            "structure_generation": None,
            "protection_policy_version": None,
            "catalog_generation": str(
                (self._catalog_state(plan) or {}).get("current_catalog_generation") or ""
            )
            or None,
            "unreadable": None,
        }
        if not option_run_id:
            return state
        try:
            from backend.options.execution.durable_store import DurableOptionRunStore

            with self.session_factory() as session:
                run = DurableOptionRunStore(
                    session_factory=lambda: session
                ).get_run(option_run_id)
            metadata = getattr(run, "metadata", None) or {}
            state["structure_generation"] = _coerce_generation(
                metadata.get("structure_generation")
            )
        except Exception as exc:  # noqa: BLE001 - unreadable is its own state
            state["unreadable"] = type(exc).__name__
        owner = self._option_owner_state(option_run_id)
        state["protection_policy_version"] = owner.get("protection_policy_version")
        if owner.get("unreadable") and not state["unreadable"]:
            state["unreadable"] = owner["unreadable"]
        return state

    def _option_owner_state(self, option_run_id: str) -> Dict[str, Any]:
        try:
            from backend.options.execution.plan_binding import (
                read_option_protection_owner,
            )

            with self.session_factory() as session:
                owner, unreadable = read_option_protection_owner(
                    option_run_id, session=session
                )
        except Exception as exc:  # noqa: BLE001 - unreadable is its own state
            return {"protection_policy_version": None, "unreadable": type(exc).__name__}
        if unreadable is not None:
            return {"protection_policy_version": None, "unreadable": str(unreadable)}
        if owner is None:
            return {"protection_policy_version": None, "unreadable": None}
        return {
            "protection_policy_version": _text_or_none(owner.get("policy_version")),
            "unreadable": None,
        }

    @staticmethod
    def _pins_equal(
        existing: StrategyApproval, pins: Mapping[str, Any], session_product: Mapping[str, Any]
    ) -> bool:
        return (
            str(existing.plan_hash) == str(pins["plan_hash"])
            and int(existing.exposure_snapshot_version or 0) == int(pins["exposure_snapshot_version"])
            and existing.exposure_snapshot_hash == pins["exposure_snapshot_hash"]
            and int(existing.reconciliation_version or 0) == int(pins["reconciliation_version"])
            and _text_or_none(existing.strategy_version_id) == pins["strategy_version_id"]
            and _int_or_none(existing.version_number) == pins["version_number"]
            and _text_or_none(existing.source_sha256) == pins["source_sha256"]
            and _text_or_none(existing.policy_hash) == pins["policy_hash"]
            and _text_or_none(existing.option_run_id) == pins["option_run_id"]
            and _int_or_none(existing.based_on_generation) == pins["based_on_generation"]
            and _text_or_none(existing.protection_policy_version)
            == pins["protection_policy_version"]
            and _int_or_none(existing.reserved_option_generation)
            == pins["reserved_option_generation"]
            and dict(existing.session_product_snapshot or {}) == dict(session_product)
        )

    @classmethod
    def _view(cls, row: StrategyApproval) -> Dict[str, Any]:
        return {
            "approval_id": str(row.approval_id),
            "plan_id": str(row.plan_id),
            "strategy_id": str(row.strategy_id),
            "account_id": str(row.account_id),
            "reservation_id": str(row.reservation_id),
            "plan_hash": str(row.plan_hash),
            "exposure_snapshot_version": int(row.exposure_snapshot_version or 0),
            "exposure_snapshot_hash": row.exposure_snapshot_hash,
            "reconciliation_version": int(row.reconciliation_version or 0),
            "catalog_generation": str(row.catalog_generation),
            "strategy_version_id": _text_or_none(row.strategy_version_id),
            "version_number": _int_or_none(row.version_number),
            "source_sha256": _text_or_none(row.source_sha256),
            "policy_hash": _text_or_none(row.policy_hash),
            "option_run_id": _text_or_none(row.option_run_id),
            "based_on_generation": _int_or_none(row.based_on_generation),
            "protection_policy_version": _text_or_none(row.protection_policy_version),
            "reserved_option_generation": _int_or_none(row.reserved_option_generation),
            "session_product_snapshot": dict(row.session_product_snapshot or {}),
            "actor_id": str(row.actor_id),
            "actor_kind": str(getattr(row, "actor_kind", None) or "manual"),
            "authorization_evidence": dict(
                getattr(row, "authorization_evidence", None) or {}
            ),
            "status": str(row.status),
            "valid_from": _as_datetime(row.valid_from).isoformat() if row.valid_from else None,
            "valid_until": _as_datetime(row.valid_until).isoformat() if row.valid_until else None,
        }
