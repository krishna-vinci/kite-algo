"""Owner-issued standing authorisation for hosted execution (Phase 2).

Three decisions live here, and they are deliberately the *only* place they live:

* **Mode is not authority.** ``approval_based`` is the default and waits for the
  operator on every plan. ``autonomous`` merely makes an owner-issued grant
  usable; selecting it grants nothing.
* **A grant is identity.** It binds the hosted strategy, the immutable version id
  and source hash, the canonical account, the execution environment, and a
  canonical hash of the validated admission policy plus the strategy's mandatory
  run-protection policy. It carries no capital or loss tolerance of its own: the
  recorded policy is hashed, and ANY policy change - including a tightening -
  invalidates the grant and requires a fresh explicit authorisation.
* **Mutation and dispatch claim use one lock.** Mode changes, grant
  issue/revoke/supersede and the dispatcher's claim acquisition all take the
  ``hosted_strategies`` row ``FOR UPDATE`` first, so "revocation won before the
  claim" and "the claim already won" are decidable rather than racy. No network
  call ever happens inside that transaction.

The child never appears in this module: grants are issued by the authenticated
owner (the operator router derives ``app:<username>`` from the browser session),
and nothing accepts an actor identity from a payload.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import Strategy, StrategyAdmissionPolicy
from backend.strategies.models import (
    HostedExecutionAudit,
    HostedExecutionGrant,
    HostedStrategy,
    HostedStrategyVersion,
)

#: The two authorization choices. ``approval_based`` is the default everywhere.
AUTHORIZATION_MODES = ("approval_based", "autonomous")
DEFAULT_AUTHORIZATION_MODE = "approval_based"

#: Execution environments a grant can be bound to.
GRANT_ENVIRONMENTS = ("paper", "dry_run", "live")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def canonical_json(value: Any) -> str:
    """Canonical JSON for hashing: sorted keys, no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class AuthorizationError(Exception):
    """A named refusal from the authorization service. Nothing was written."""

    reason_code = "AUTHORIZATION_ERROR"
    status_code = 409

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


class AuthorizationInputError(AuthorizationError):
    reason_code = "AUTHORIZATION_INPUT_INVALID"
    status_code = 422


class AuthorizationModeError(AuthorizationError):
    """The strategy is not in ``autonomous`` mode, so a grant is unusable."""

    reason_code = "AUTHORIZATION_MODE_NOT_AUTONOMOUS"
    status_code = 409


class AuthorizationKeyConflict(AuthorizationError):
    """The idempotency key was already used for different content."""

    reason_code = "AUTHORIZATION_KEY_CONFLICT"
    status_code = 409


class AuthorizationPolicyIncomplete(AuthorizationError):
    """No concrete validated capital/risk policy exists to bind."""

    reason_code = "AUTHORIZATION_POLICY_INCOMPLETE"
    status_code = 409


class GrantNotActive(AuthorizationError):
    reason_code = "GRANT_NOT_ACTIVE"
    status_code = 409


class GrantNotFound(AuthorizationError):
    reason_code = "GRANT_NOT_FOUND"
    status_code = 404


class ExecutionAuthorizationService:
    """Mode, grant and policy-evidence decisions for one hosted strategy."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # -- policy evidence ----------------------------------------------------

    def protection_policy(self, strategy: Any) -> Dict[str, Any]:
        """The strategy's mandatory run-protection policy, as it is pinned."""
        return {
            "stale_exit_policy": str(getattr(strategy, "stale_exit_policy", "") or ""),
            "max_duration_s": int(getattr(strategy, "max_duration_s", 0) or 0),
            "progress_deadline_s": int(getattr(strategy, "progress_deadline_s", 0) or 0),
        }

    @staticmethod
    def _admission_evidence(row: Any) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "account_id": str(row.account_id),
            "allocation_inr": None if row.allocation_inr is None else float(row.allocation_inr),
            "per_instrument_notional_inr": (
                None
                if row.per_instrument_notional_inr is None
                else float(row.per_instrument_notional_inr)
            ),
            "gross_notional_inr": (
                None if row.gross_notional_inr is None else float(row.gross_notional_inr)
            ),
            "max_open_instruments": (
                None if row.max_open_instruments is None else int(row.max_open_instruments)
            ),
            "admissions_per_window": (
                None if row.admissions_per_window is None else int(row.admissions_per_window)
            ),
            "admission_window_seconds": (
                None
                if row.admission_window_seconds is None
                else int(row.admission_window_seconds)
            ),
            "daily_loss_budget_inr": (
                None if row.daily_loss_budget_inr is None else float(row.daily_loss_budget_inr)
            ),
        }

    def policy_snapshot(self, session: Any, strategy: Any) -> Dict[str, Any]:
        """The exact policy basis a grant binds: admission + run protection.

        Neither half is invented here: the admission half is the owner's recorded
        policy row (or an explicit ``None`` when none exists) and the protection
        half is the strategy's own pinned values. The canonical hash of this
        object is what a grant records.
        """
        row = session.execute(
            select(StrategyAdmissionPolicy).where(
                StrategyAdmissionPolicy.strategy_id == str(strategy.id)
            )
        ).scalar_one_or_none()
        return {
            "admission": self._admission_evidence(row),
            "protection": self.protection_policy(strategy),
        }

    @staticmethod
    def policy_hash_for(snapshot: Mapping[str, Any]) -> str:
        return sha256_json(dict(snapshot))

    def policy_basis(self, owner_id: str, strategy_id: str) -> Dict[str, Any]:
        """Current policy snapshot + hash for one owned strategy."""
        with self.session_factory() as session:
            strategy = self._owned_strategy(session, owner_id, strategy_id)
            if strategy is None:
                raise GrantNotFound({"strategy_id": str(strategy_id)})
            snapshot = self.policy_snapshot(session, strategy)
            return {
                "policy_snapshot": snapshot,
                "policy_hash": self.policy_hash_for(snapshot),
                "concrete": self._policy_concrete(snapshot),
            }

    @staticmethod
    def _policy_concrete(snapshot: Mapping[str, Any]) -> bool:
        """A grant needs a REAL capital basis; an unset limit is not a limit.

        Only the recorded admission allocation is required, because that is the
        one field the platform already treats as mandatory for a live strategy.
        Nothing is inferred: a missing allocation refuses the grant instead of
        inventing a user's capital.
        """
        admission = dict(snapshot.get("admission") or {})
        allocation = admission.get("allocation_inr")
        try:
            return allocation is not None and float(allocation) > 0
        except (TypeError, ValueError):
            return False

    # -- reads --------------------------------------------------------------

    @staticmethod
    def _owned_strategy(session: Any, owner_id: str, strategy_id: str) -> Optional[HostedStrategy]:
        return session.execute(
            select(HostedStrategy).where(
                HostedStrategy.id == str(strategy_id),
                HostedStrategy.owner_id == str(owner_id),
            )
        ).scalar_one_or_none()

    def authorization_mode(self, strategy_id: str) -> Optional[str]:
        with self.session_factory() as session:
            return session.execute(
                select(HostedStrategy.authorization_mode).where(
                    HostedStrategy.id == str(strategy_id)
                )
            ).scalar_one_or_none()

    def active_grant(
        self,
        strategy_id: str,
        *,
        account_id: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            return self._active_grant_view(
                session, strategy_id, account_id=account_id, environment=environment
            )

    def _active_grant_view(
        self,
        session: Any,
        strategy_id: str,
        *,
        account_id: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        stmt = select(HostedExecutionGrant).where(
            HostedExecutionGrant.strategy_id == str(strategy_id),
            HostedExecutionGrant.status == "active",
        )
        if account_id is not None:
            stmt = stmt.where(HostedExecutionGrant.account_id == str(account_id))
        if environment is not None:
            stmt = stmt.where(HostedExecutionGrant.execution_environment == str(environment))
        row = (
            session.execute(stmt.order_by(HostedExecutionGrant.created_at.desc()))
            .scalars()
            .first()
        )
        return self._grant_view(row) if row is not None else None

    def get_grant(self, grant_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(HostedExecutionGrant).where(
                    HostedExecutionGrant.grant_id == str(grant_id)
                )
            ).scalar_one_or_none()
            return self._grant_view(row) if row is not None else None

    def list_grants(self, strategy_id: str, *, limit: int = 50) -> list:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(HostedExecutionGrant)
                    .where(HostedExecutionGrant.strategy_id == str(strategy_id))
                    .order_by(
                        HostedExecutionGrant.created_at.desc(),
                        HostedExecutionGrant.grant_id,
                    )
                    .limit(int(limit))
                )
                .scalars()
                .all()
            )
            return [self._grant_view(row) for row in rows]

    def audit_trail(self, strategy_id: str, *, limit: int = 100) -> list:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(HostedExecutionAudit)
                    .where(HostedExecutionAudit.strategy_id == str(strategy_id))
                    .order_by(HostedExecutionAudit.audit_id.desc())
                    .limit(int(limit))
                )
                .scalars()
                .all()
            )
            return [
                {
                    "audit_id": int(row.audit_id),
                    "strategy_id": str(row.strategy_id),
                    "subject_kind": str(row.subject_kind),
                    "subject_id": str(row.subject_id),
                    "event": str(row.event),
                    "actor_id": str(row.actor_id),
                    "actor_kind": str(row.actor_kind),
                    "detail": dict(row.detail or {}),
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    @staticmethod
    def _grant_view(row: HostedExecutionGrant) -> Dict[str, Any]:
        return {
            "grant_id": str(row.grant_id),
            "owner_id": str(row.owner_id),
            "strategy_id": str(row.strategy_id),
            "canonical_strategy_id": str(row.canonical_strategy_id),
            "version_id": str(row.version_id),
            "version_number": int(row.version_number),
            "source_sha256": str(row.source_sha256),
            "account_id": str(row.account_id),
            "execution_environment": str(row.execution_environment),
            "policy_hash": str(row.policy_hash),
            "policy_snapshot": dict(row.policy_snapshot or {}),
            "issued_by": str(row.issued_by),
            "issued_at": row.issued_at,
            "expires_at": row.expires_at,
            "status": str(row.status),
            "revoked_by": row.revoked_by,
            "revoked_at": row.revoked_at,
            "revocation_reason": row.revocation_reason,
            "superseded_by": row.superseded_by,
            "superseded_at": row.superseded_at,
            "supersession_reason": row.supersession_reason,
            "request_key": str(row.request_key),
            "content_sha256": str(row.content_sha256),
            "created_at": row.created_at,
        }

    def status(
        self, owner_id: str, strategy_id: str, *, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """The operator's authorization view: mode, grant, policy, refusals.

        Reads are pure: nothing here writes, mutates or dispatches. The returned
        ``grant_usable`` answer is a statement about the current record, not the
        execution-time decision, which is re-derived under the strategy lock.
        """
        moment = now or _utcnow()
        with self.session_factory() as session:
            strategy = self._owned_strategy(session, owner_id, strategy_id)
            if strategy is None:
                raise GrantNotFound({"strategy_id": str(strategy_id)})
            snapshot = self.policy_snapshot(session, strategy)
            policy_hash = self.policy_hash_for(snapshot)
            grant = self._active_grant_view(session, str(strategy.id))
            mode = str(strategy.authorization_mode or DEFAULT_AUTHORIZATION_MODE)
            reasons: list = []
            if not self._policy_concrete(snapshot):
                reasons.append("AUTHORIZATION_POLICY_INCOMPLETE")
            if mode != "autonomous":
                reasons.append("AUTHORIZATION_MODE_NOT_AUTONOMOUS")
            elif grant is None:
                reasons.append("GRANT_REQUIRED")
            else:
                refusal = self._grant_refusal(
                    grant,
                    mode=mode,
                    version_id=str(grant["version_id"]),
                    source_sha256=str(grant["source_sha256"]),
                    policy_hash=policy_hash,
                    now=moment,
                )
                if refusal is not None:
                    reasons.append(refusal)
            return {
                "strategy_id": str(strategy.id),
                "authorization_mode": mode,
                "active_grant": grant,
                "policy_snapshot": snapshot,
                "policy_hash": policy_hash,
                "policy_concrete": self._policy_concrete(snapshot),
                "grant_usable": not reasons,
                "blocking_reasons": reasons,
                "evaluated_at": moment,
            }

    # -- mode ---------------------------------------------------------------

    def set_mode(
        self,
        owner_id: str,
        strategy_id: str,
        mode: str,
        *,
        actor: str,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Set the authorization mode under the strategy row lock.

        Moving to ``approval_based`` supersedes any active grant in the SAME
        transaction: the grant's own validation would refuse it anyway, but
        leaving it ``active`` while unusable invites a reader to believe an
        autonomous mandate is still standing. Moving to ``autonomous`` issues
        nothing - a grant is a separate, explicit owner act.
        """
        moment = now or _utcnow()
        wanted = str(mode or "").strip().lower()
        if wanted not in AUTHORIZATION_MODES:
            raise AuthorizationInputError(
                {"mode": str(mode), "supported": list(AUTHORIZATION_MODES)}
            )
        session = self.session_factory()
        try:
            strategy = self._lock_owned_strategy(session, owner_id, strategy_id)
            if strategy is None:
                raise GrantNotFound({"strategy_id": str(strategy_id)})
            previous = str(strategy.authorization_mode or DEFAULT_AUTHORIZATION_MODE)
            if previous != wanted:
                strategy.authorization_mode = wanted
                session.add(
                    HostedExecutionAudit(
                        owner_id=str(owner_id),
                        strategy_id=str(strategy_id),
                        subject_kind="mode",
                        subject_id=str(strategy_id),
                        event="mode_changed",
                        actor_id=str(actor),
                        actor_kind="owner",
                        detail={
                            "previous_mode": previous,
                            "authorization_mode": wanted,
                            "reason": reason,
                        },
                        created_at=moment,
                    )
                )
                if wanted != "autonomous":
                    for superseded in self._supersede_active(
                        session,
                        strategy_id=str(strategy_id),
                        reason="authorization_mode_changed",
                        replacement_id=None,
                        moment=moment,
                    ):
                        session.add(
                            HostedExecutionAudit(
                                owner_id=str(owner_id),
                                strategy_id=str(strategy_id),
                                subject_kind="grant",
                                subject_id=str(superseded),
                                event="superseded",
                                actor_id=str(actor),
                                actor_kind="owner",
                                detail={"reason": "authorization_mode_changed"},
                                created_at=moment,
                            )
                        )
            session.commit()
            return {
                "strategy_id": str(strategy_id),
                "authorization_mode": str(strategy.authorization_mode),
                "previous_mode": previous,
                "changed": previous != wanted,
            }
        except AuthorizationError:
            session.rollback()
            raise
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    # -- grants -------------------------------------------------------------

    def issue_grant(
        self,
        owner_id: str,
        strategy_id: str,
        *,
        actor: str,
        idempotency_key: str,
        version_id: str,
        execution_environment: str,
        expires_at: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Issue one immutable grant, or replay an identical earlier request.

        Everything that identifies the grant except the environment is derived
        from persisted records (the version row and its stored source hash, the
        canonical strategy's account, the recorded policy). The environment is
        owner-chosen but validated against the grant vocabulary, and a ``live``
        grant is refused while the deployment has hosted live disabled.
        """
        moment = now or _utcnow()
        key = str(idempotency_key or "").strip()
        if not key:
            raise AuthorizationInputError({"message": "idempotency_key is required"})
        environment = str(execution_environment or "").strip().lower()
        if environment not in GRANT_ENVIRONMENTS:
            raise AuthorizationInputError(
                {
                    "execution_environment": str(execution_environment),
                    "supported": list(GRANT_ENVIRONMENTS),
                }
            )
        expiry = _as_utc(expires_at)
        if expiry is not None and expiry <= moment:
            raise AuthorizationInputError(
                {
                    "expires_at": expiry.isoformat(),
                    "message": "expires_at must be in the future",
                }
            )

        session = self.session_factory()
        try:
            strategy = self._lock_owned_strategy(session, owner_id, strategy_id)
            if strategy is None:
                raise GrantNotFound({"strategy_id": str(strategy_id)})
            mode = str(strategy.authorization_mode or DEFAULT_AUTHORIZATION_MODE)
            if mode != "autonomous":
                raise AuthorizationModeError(
                    {
                        "strategy_id": str(strategy_id),
                        "authorization_mode": mode,
                        "message": (
                            "select autonomous mode before issuing a grant; an autonomous "
                            "selection alone still grants nothing until this call succeeds"
                        ),
                    }
                )

            version = session.execute(
                select(HostedStrategyVersion).where(
                    HostedStrategyVersion.id == str(version_id),
                    HostedStrategyVersion.strategy_id == str(strategy_id),
                )
            ).scalar_one_or_none()
            if version is None:
                raise AuthorizationInputError(
                    {
                        "version_id": str(version_id),
                        "message": "version does not belong to this strategy",
                    }
                )

            snapshot = self.policy_snapshot(session, strategy)
            policy_hash = self.policy_hash_for(snapshot)
            if not self._policy_concrete(snapshot):
                raise AuthorizationPolicyIncomplete(
                    {
                        "strategy_id": str(strategy_id),
                        "policy_hash": policy_hash,
                        "message": (
                            "record a concrete admission policy (allocation_inr) before "
                            "requesting autonomous execution; unset limits are not limits"
                        ),
                    }
                )

            canonical = session.execute(
                select(Strategy).where(
                    Strategy.id == str(strategy_id),
                    Strategy.owner_id == str(owner_id),
                )
            ).scalar_one_or_none()
            if canonical is None:
                raise AuthorizationInputError(
                    {
                        "strategy_id": str(strategy_id),
                        "message": "strategy has no canonical identity",
                    }
                )
            account_id = str(canonical.account_scope)

            content = {
                "strategy_id": str(strategy_id),
                "canonical_strategy_id": str(canonical.id),
                "version_id": str(version.id),
                "version_number": int(version.version),
                "source_sha256": str(version.source_sha256),
                "account_id": account_id,
                "execution_environment": environment,
                "policy_hash": policy_hash,
                "expires_at": expiry.isoformat() if expiry else None,
            }
            content_hash = sha256_json(content)

            existing = session.execute(
                select(HostedExecutionGrant).where(
                    HostedExecutionGrant.owner_id == str(owner_id),
                    HostedExecutionGrant.strategy_id == str(strategy_id),
                    HostedExecutionGrant.request_key == key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if str(existing.content_sha256) != content_hash:
                    raise AuthorizationKeyConflict(
                        {
                            "grant_id": str(existing.grant_id),
                            "request_key": key,
                            "message": (
                                "this idempotency key was already used for a different grant "
                                "request; a revoked grant is never resurrected"
                            ),
                        }
                    )
                view = self._grant_view(existing)
                session.rollback()
                return {"idempotent": True, "grant": view, "policy_hash": str(view["policy_hash"])}

            if environment == "live":
                self._refuse_live_when_disabled(strategy_id=str(strategy_id))

            grant_id = str(uuid.uuid4())
            superseded = self._supersede_active(
                session,
                strategy_id=str(strategy_id),
                reason="replaced_by_new_grant",
                replacement_id=grant_id,
                moment=moment,
            )
            row = HostedExecutionGrant(
                grant_id=grant_id,
                owner_id=str(owner_id),
                strategy_id=str(strategy_id),
                canonical_strategy_id=str(canonical.id),
                version_id=str(version.id),
                version_number=int(version.version),
                source_sha256=str(version.source_sha256),
                account_id=account_id,
                execution_environment=environment,
                policy_hash=policy_hash,
                policy_snapshot=snapshot,
                issued_by=str(actor),
                issued_at=moment,
                expires_at=expiry,
                status="active",
                request_key=key,
                content_sha256=content_hash,
                created_at=moment,
            )
            session.add(row)
            for superseded_id in superseded:
                session.add(
                    HostedExecutionAudit(
                        owner_id=str(owner_id),
                        strategy_id=str(strategy_id),
                        subject_kind="grant",
                        subject_id=str(superseded_id),
                        event="superseded",
                        actor_id=str(actor),
                        actor_kind="owner",
                        detail={
                            "reason": "replaced_by_new_grant",
                            "superseded_by": grant_id,
                        },
                        created_at=moment,
                    )
                )
            session.add(
                HostedExecutionAudit(
                    owner_id=str(owner_id),
                    strategy_id=str(strategy_id),
                    subject_kind="grant",
                    subject_id=grant_id,
                    event="granted",
                    actor_id=str(actor),
                    actor_kind="owner",
                    detail={
                        "version_id": str(version.id),
                        "version_number": int(version.version),
                        "source_sha256": str(version.source_sha256),
                        "account_id": account_id,
                        "execution_environment": environment,
                        "policy_hash": policy_hash,
                        "expires_at": expiry.isoformat() if expiry else None,
                        "request_key": key,
                    },
                    created_at=moment,
                )
            )
            session.commit()
            return {
                "idempotent": False,
                "grant": self._grant_view(row),
                "policy_hash": policy_hash,
            }
        except AuthorizationError:
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            raise AuthorizationKeyConflict(
                {"request_key": key, "message": "a concurrent grant request won this key"}
            ) from exc
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    def revoke_grant(
        self,
        owner_id: str,
        strategy_id: str,
        *,
        actor: str,
        reason: Optional[str] = None,
        grant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Revoke one grant under the strategy row lock.

        The lock is what makes "revocation won before the dispatch claim" a fact
        rather than a race: the claim takes the same row before it decides. It
        does not and cannot cancel an order already at the broker.
        """
        moment = now or _utcnow()
        session = self.session_factory()
        try:
            strategy = self._lock_owned_strategy(session, owner_id, strategy_id)
            if strategy is None:
                raise GrantNotFound({"strategy_id": str(strategy_id)})
            stmt = select(HostedExecutionGrant).where(
                HostedExecutionGrant.strategy_id == str(strategy_id)
            )
            if grant_id:
                stmt = stmt.where(HostedExecutionGrant.grant_id == str(grant_id))
            else:
                stmt = stmt.where(HostedExecutionGrant.status == "active")
            row = (
                session.execute(stmt.order_by(HostedExecutionGrant.created_at.desc()))
                .scalars()
                .first()
            )
            if row is None:
                raise GrantNotFound({"strategy_id": str(strategy_id), "grant_id": grant_id})
            if str(row.status) != "active":
                raise GrantNotActive({"grant_id": str(row.grant_id), "status": str(row.status)})
            row.status = "revoked"
            row.revoked_by = str(actor)
            row.revoked_at = moment
            row.revocation_reason = reason
            session.add(
                HostedExecutionAudit(
                    owner_id=str(owner_id),
                    strategy_id=str(strategy_id),
                    subject_kind="grant",
                    subject_id=str(row.grant_id),
                    event="revoked",
                    actor_id=str(actor),
                    actor_kind="owner",
                    detail={"reason": reason, "policy_hash": str(row.policy_hash)},
                    created_at=moment,
                )
            )
            session.commit()
            return {"grant": self._grant_view(row), "revoked_at": moment}
        except AuthorizationError:
            session.rollback()
            raise
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    # -- execution-time evaluation -----------------------------------------

    @staticmethod
    def _latest_grant(
        session: Any, strategy_id: str, grant_id: Optional[str]
    ) -> Optional[HostedExecutionGrant]:
        """The grant the decision is about.

        With an explicit id, that row (whatever its state - the refusal must NAME
        "revoked" rather than degrade to "required"). Without one, the newest
        grant for the strategy: the partial unique index guarantees at most one
        of them is ``active``, and reporting the newest is what makes a
        revocation or a supersession legible instead of anonymous.
        """
        stmt = select(HostedExecutionGrant).where(
            HostedExecutionGrant.strategy_id == str(strategy_id)
        )
        if grant_id:
            stmt = stmt.where(HostedExecutionGrant.grant_id == str(grant_id))
        return (
            session.execute(
                stmt.order_by(
                    HostedExecutionGrant.created_at.desc(),
                    HostedExecutionGrant.grant_id,
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    def _grant_refusal(
        grant: Mapping[str, Any],
        *,
        mode: str,
        version_id: str,
        source_sha256: str,
        policy_hash: str,
        now: datetime,
    ) -> Optional[str]:
        if mode != "autonomous":
            return "AUTHORIZATION_MODE_NOT_AUTONOMOUS"
        status = str(grant.get("status") or "")
        if status == "revoked":
            return "GRANT_REVOKED"
        if status == "superseded":
            return "GRANT_SUPERSEDED"
        if status != "active":
            return "GRANT_NOT_ACTIVE"
        expires_at = _as_utc(grant.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            return "GRANT_EXPIRED"
        if str(grant.get("version_id") or "") != str(version_id):
            return "GRANT_VERSION_MISMATCH"
        if str(grant.get("source_sha256") or "") != str(source_sha256):
            return "GRANT_SOURCE_CHANGED"
        if str(grant.get("policy_hash") or "") != str(policy_hash):
            return "GRANT_POLICY_CHANGED"
        return None

    def evaluate(
        self,
        *,
        strategy_id: str,
        mode: str,
        account_id: str,
        environment: str,
        version_id: str,
        source_sha256: str,
        policy_hash: str,
        grant_id: Optional[str] = None,
        now: Optional[datetime] = None,
        session: Any = None,
    ) -> Dict[str, Any]:
        """Re-derive whether a grant still authorises this exact work.

        Called at request creation, at dispatch claim acquisition, and again on
        every dependent release. It reads persisted records only.

        ``session`` lets a caller that already holds the strategy lock (the
        dispatch claim) re-derive through ITS OWN transaction, so the authority
        decision and the claim commit together instead of racing a second
        connection.
        """
        moment = now or _utcnow()
        if str(mode) != "autonomous":
            return {
                "authorized": False,
                "refusal_code": "AUTHORIZATION_MODE_NOT_AUTONOMOUS",
                "grant": None,
                "evaluated_at": moment,
            }
        if session is not None:
            row = self._latest_grant(session, str(strategy_id), grant_id)
        else:
            with self.session_factory() as own_session:
                row = self._latest_grant(own_session, str(strategy_id), grant_id)
        if row is None:
            return {
                "authorized": False,
                "refusal_code": "GRANT_REQUIRED",
                "grant": None,
                "evaluated_at": moment,
            }
        view = self._grant_view(row)
        refusal = self._grant_refusal(
            view,
            mode=str(mode),
            version_id=str(version_id),
            source_sha256=str(source_sha256),
            policy_hash=str(policy_hash),
            now=moment,
        )
        if refusal is None and (
            str(view.get("account_id") or "") != str(account_id)
            or str(view.get("execution_environment") or "") != str(environment)
        ):
            refusal = "GRANT_ACCOUNT_MISMATCH"
        return {
            "authorized": refusal is None,
            "refusal_code": refusal,
            "grant": view,
            "evaluated_at": moment,
        }

    # -- internals ----------------------------------------------------------

    def _lock_owned_strategy(
        self, session: Any, owner_id: str, strategy_id: str
    ) -> Optional[HostedStrategy]:
        """The single lock target for governed writes (``FOR UPDATE``)."""
        return session.execute(
            select(HostedStrategy)
            .where(
                HostedStrategy.id == str(strategy_id),
                HostedStrategy.owner_id == str(owner_id),
            )
            .with_for_update()
        ).scalar_one_or_none()

    def lock_strategy(self, session: Any, strategy_id: str) -> Optional[HostedStrategy]:
        """The same lock without the owner filter (the dispatcher is not a caller)."""
        return session.execute(
            select(HostedStrategy)
            .where(HostedStrategy.id == str(strategy_id))
            .with_for_update()
        ).scalar_one_or_none()

    def _supersede_active(
        self,
        session: Any,
        *,
        strategy_id: str,
        reason: str,
        replacement_id: Optional[str],
        moment: datetime,
    ) -> list:
        rows = (
            session.execute(
                select(HostedExecutionGrant).where(
                    HostedExecutionGrant.strategy_id == str(strategy_id),
                    HostedExecutionGrant.status == "active",
                )
            )
            .scalars()
            .all()
        )
        superseded_ids = []
        for row in rows:
            row.status = "superseded"
            row.superseded_by = replacement_id or str(row.grant_id)
            row.superseded_at = moment
            row.supersession_reason = reason
            superseded_ids.append(str(row.grant_id))
        return superseded_ids

    @staticmethod
    def _refuse_live_when_disabled(*, strategy_id: str) -> None:
        from backend.strategies.live_settings import hosted_live_enabled

        if hosted_live_enabled():
            return
        raise AuthorizationError(
            {
                "rejection_reason": "LIVE_DISABLED",
                "strategy_id": strategy_id,
                "setting": "HOSTED_LIVE_ENABLED",
                "message": (
                    "hosted live execution is disabled in this deployment; a live grant "
                    "would have nothing to authorise"
                ),
            }
        )
