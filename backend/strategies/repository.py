"""Durable store and fenced state transitions for hosted strategies.

Design rules, all deliberate:

- **Owner-scoped reads.** Every read is filtered by ``owner_id``; a foreign id is
  ``None`` (the router renders 404), so an id owned by another operator never
  leaks existence.
- **Versions are immutable.** There is no update/delete path; numbering is
  transactional (the strategy row is locked, then ``max(version)+1``), and
  ``UNIQUE(strategy_id, version)`` is the backstop.
- **Job creation enforces the replacement block IN TRANSACTION.** ``create_job``
  locks the strategy row, validates that the version belongs to the strategy and
  the owner matches, then refuses while any job of that strategy is
  ``queued``/``starting``/``running`` or is an unreconciled
  ``recovery_required``. Recovery/reconciliation lock the same strategy row
  first, so creation and recovery serialise and a race cannot bypass the block.
- **Immutable snapshots are built by the store, not the caller.** ``create_job``
  validates params against the pinned version schema, derives the capability
  snapshot from the version, and pins ``account_scope`` plus the effective
  ``max_duration_s``/``progress_deadline_s`` from the strategy, deep-copied, so a
  queued job can never be reconstructed from later defaults.
- **Fencing matches id + lease_owner + lease_epoch + attempt (+ state).** Every
  authority-bearing transition compares all four; a stale holder is a no-op.
- **Recovery is durable and blocks replacement.** ``mark_recovery_required`` and
  ``expire_to_recovery`` commit in their OWN transaction, so a later failing
  effect cannot roll the fence back.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.strategies.models import (
    HostedStrategy,
    HostedStrategySchedule,
    HostedStrategyVersion,
    StrategyJob,
)
from backend.strategies import service

__all__ = [
    "SqlAlchemyStrategyRepository",
    "StrategyConflict",
    "StrategyDisabled",
    "StrategyFenceError",
    "StrategyIdentityError",
    "StrategyNotFound",
]

_UNRECONCILED = "recovery_required"
_ACTIVE_JOB_STATUSES = ("queued", "starting", "running")
_UNSET = object()


class StrategyConflict(Exception):
    """A unique constraint was violated (duplicate name/version/occurrence)."""


class StrategyFenceError(Exception):
    """A fenced transition was refused (stale authority or unreconciled recovery)."""


class StrategyNotFound(Exception):
    """The strategy does not exist for this owner."""


class StrategyIdentityError(Exception):
    """A referenced version/owner does not belong to the strategy."""


class StrategyDisabled(Exception):
    """A job may not be created or claimed for a disabled strategy.

    Disable is metadata only in this slice: it stops NEW attempts, it does not
    stop an already-running job (there is no runner to stop it).
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyStrategyRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def _session(self) -> Session:
        session = self.session_factory()
        session.expire_on_commit = False
        return session

    @staticmethod
    def _lock_strategy(session: Session, strategy_id: str, owner_id: Optional[str] = None):
        stmt = select(HostedStrategy).where(HostedStrategy.id == strategy_id)
        if owner_id is not None:
            stmt = stmt.where(HostedStrategy.owner_id == owner_id)
        return session.execute(stmt.with_for_update()).scalar_one_or_none()

    @staticmethod
    def _strategy_id_for_job(session: Session, job_id: str) -> Optional[str]:
        return session.execute(
            select(StrategyJob.strategy_id).where(StrategyJob.id == job_id)
        ).scalar_one_or_none()

    # -- strategies ---------------------------------------------------------

    def create_strategy(
        self,
        *,
        owner_id: str,
        name: str,
        description: Optional[str],
        execution_mode: str,
        job_kind: str,
        account_scope: str,
        max_duration_s: int,
        progress_deadline_s: int,
        stale_exit_policy: str,
    ) -> HostedStrategy:
        strategy_id = service.new_strategy_id()
        row = HostedStrategy(
            id=strategy_id,
            owner_id=owner_id,
            name=name,
            template_id=service.template_id_for(strategy_id),
            description=description,
            default_execution_mode=execution_mode,
            default_job_kind=job_kind,
            default_account_scope=account_scope,
            max_duration_s=max_duration_s,
            progress_deadline_s=progress_deadline_s,
            stale_exit_policy=stale_exit_policy,
            status="active",
        )
        session = self._session()
        try:
            session.add(row)
            session.commit()
            return row
        except IntegrityError as exc:
            session.rollback()
            raise StrategyConflict("a strategy with this name already exists") from exc
        finally:
            session.close()

    def get_strategy(self, owner_id: str, strategy_id: str) -> Optional[HostedStrategy]:
        session = self._session()
        try:
            return session.execute(
                select(HostedStrategy).where(
                    HostedStrategy.id == strategy_id,
                    HostedStrategy.owner_id == owner_id,
                )
            ).scalar_one_or_none()
        finally:
            session.close()

    def list_strategies(self, owner_id: str) -> List[HostedStrategy]:
        session = self._session()
        try:
            return list(
                session.execute(
                    select(HostedStrategy)
                    .where(HostedStrategy.owner_id == owner_id)
                    .order_by(HostedStrategy.created_at, HostedStrategy.id)
                ).scalars()
            )
        finally:
            session.close()

    def update_strategy(
        self,
        owner_id: str,
        strategy_id: str,
        *,
        description: Any = _UNSET,
        status: Any = _UNSET,
    ) -> Optional[HostedStrategy]:
        """Minimal owner-scoped metadata update. Versions are never touched.

        Returns ``None`` for a foreign/missing id (router renders 404).
        """
        if status is not _UNSET and status not in ("active", "disabled"):
            raise service.StrategyValidationError("status must be 'active' or 'disabled'")
        session = self._session()
        try:
            row = self._lock_strategy(session, strategy_id, owner_id)
            if row is None:
                session.rollback()
                return None
            if description is not _UNSET:
                row.description = description
            if status is not _UNSET:
                row.status = status
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- versions (immutable) -----------------------------------------------

    def create_version(
        self,
        *,
        strategy_id: str,
        source: str,
        source_sha256: str,
        parameters_schema: Dict[str, Any],
        capabilities_snapshot: Dict[str, Any],
        created_by: str,
    ) -> HostedStrategyVersion:
        """Append the next immutable version under a row lock."""
        session = self._session()
        try:
            session.execute(
                select(HostedStrategy.id).where(HostedStrategy.id == strategy_id).with_for_update()
            ).scalar_one_or_none()
            current_max = session.execute(
                select(HostedStrategyVersion.version)
                .where(HostedStrategyVersion.strategy_id == strategy_id)
                .order_by(HostedStrategyVersion.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            next_version = int(current_max or 0) + 1
            row = HostedStrategyVersion(
                id=service.new_version_id(),
                strategy_id=strategy_id,
                version=next_version,
                source=source,
                source_sha256=source_sha256,
                parameters_schema=copy.deepcopy(dict(parameters_schema or {})),
                capabilities_snapshot=copy.deepcopy(dict(capabilities_snapshot or {})),
                created_by=created_by,
            )
            session.add(row)
            session.commit()
            return row
        except IntegrityError as exc:
            session.rollback()
            raise StrategyConflict("version numbering conflict; retry") from exc
        finally:
            session.close()

    def get_version(self, strategy_id: str, version: int) -> Optional[HostedStrategyVersion]:
        session = self._session()
        try:
            return session.execute(
                select(HostedStrategyVersion).where(
                    HostedStrategyVersion.strategy_id == strategy_id,
                    HostedStrategyVersion.version == version,
                )
            ).scalar_one_or_none()
        finally:
            session.close()

    def get_version_by_id(
        self, strategy_id: str, version_id: str
    ) -> Optional[HostedStrategyVersion]:
        session = self._session()
        try:
            return session.execute(
                select(HostedStrategyVersion).where(
                    HostedStrategyVersion.strategy_id == strategy_id,
                    HostedStrategyVersion.id == version_id,
                )
            ).scalar_one_or_none()
        finally:
            session.close()

    def list_versions(self, strategy_id: str) -> List[HostedStrategyVersion]:
        session = self._session()
        try:
            return list(
                session.execute(
                    select(HostedStrategyVersion)
                    .where(HostedStrategyVersion.strategy_id == strategy_id)
                    .order_by(HostedStrategyVersion.version)
                ).scalars()
            )
        finally:
            session.close()

    # -- jobs (ledger + fencing) --------------------------------------------

    def create_job(
        self,
        *,
        strategy_id: str,
        version_id: str,
        owner_id: str,
        job_kind: str,
        execution_mode: str,
        params: Optional[Dict[str, Any]] = None,
        occurrence_key: Optional[str] = None,
        attempt: int = 1,
        desired_state: str = "started",
    ) -> StrategyJob:
        """Create a queued job, enforcing identity, snapshots and the block.

        The whole check + insert runs in one transaction that first locks the
        strategy row, so a concurrent recovery/reconciliation serialises with it
        and a race cannot bypass the block.
        """
        if job_kind not in service.ALLOWED_JOB_KINDS:
            raise service.StrategyValidationError("unsupported job_kind")
        if execution_mode not in service.ALLOWED_EXECUTION_MODES:
            raise service.StrategyValidationError("unsupported execution_mode")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise service.StrategyValidationError("attempt must be an integer >= 1")

        session = self._session()
        try:
            strategy = self._lock_strategy(session, strategy_id, owner_id)
            if strategy is None:
                raise StrategyNotFound("strategy not found for this owner")
            # Disable stops NEW attempts; the locked parent row serialises this
            # with update_strategy/disable.
            if strategy.status != "active":
                raise StrategyDisabled("strategy is disabled")
            # The PINNED account scope must be valid for the requested mode, not
            # merely for the strategy's default mode.
            service.validate_account_scope(strategy.default_account_scope, execution_mode)
            version = session.execute(
                select(HostedStrategyVersion).where(
                    HostedStrategyVersion.id == version_id,
                    HostedStrategyVersion.strategy_id == strategy_id,
                )
            ).scalar_one_or_none()
            if version is None:
                raise StrategyIdentityError("version does not belong to this strategy")

            blocking = session.execute(
                select(StrategyJob.id).where(
                    StrategyJob.owner_id == owner_id,
                    StrategyJob.strategy_id == strategy_id,
                    or_(
                        StrategyJob.status.in_(_ACTIVE_JOB_STATUSES),
                        and_(
                            StrategyJob.status == _UNRECONCILED,
                            StrategyJob.reconciled_at.is_(None),
                        ),
                    ),
                )
            ).first()
            if blocking is not None:
                raise StrategyFenceError(
                    "strategy has an active or unreconciled job; reconcile before a new attempt"
                )

            params_snapshot = service.validate_parameters(version.parameters_schema, params)
            policy_snapshot = service.build_policy_snapshot(
                stale_exit_policy=strategy.stale_exit_policy,
                max_duration_s=strategy.max_duration_s,
                progress_deadline_s=strategy.progress_deadline_s,
            )
            row = StrategyJob(
                id=service.new_job_id(),
                strategy_id=strategy_id,
                version_id=version_id,
                owner_id=owner_id,
                account_scope=strategy.default_account_scope,
                job_kind=job_kind,
                execution_mode=execution_mode,
                desired_state=desired_state,
                occurrence_key=occurrence_key,
                attempt=attempt,
                status="queued",
                params_snapshot=copy.deepcopy(params_snapshot),
                capabilities_snapshot=copy.deepcopy(dict(version.capabilities_snapshot or {})),
                policy_snapshot=copy.deepcopy(policy_snapshot),
                max_duration_s=strategy.max_duration_s,
                progress_deadline_s=strategy.progress_deadline_s,
            )
            session.add(row)
            session.commit()
            return row
        except IntegrityError as exc:
            session.rollback()
            raise StrategyConflict("occurrence_key already exists") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_job(self, owner_id: str, job_id: str) -> Optional[StrategyJob]:
        session = self._session()
        try:
            return session.execute(
                select(StrategyJob).where(
                    StrategyJob.id == job_id, StrategyJob.owner_id == owner_id
                )
            ).scalar_one_or_none()
        finally:
            session.close()

    def claim_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        lease_until: datetime,
    ):
        """CAS-claim a queued job for a supervisor.

        Matches id + epoch + attempt, requires a free/expired lease, and rejects a
        blank holder or a non-future deadline. A stale epoch/attempt loses. Only
        queued jobs are claimable: an expired starting/running lease is NOT
        re-claimed (no reattach/resurrection).
        """
        holder = str(lease_owner or "").strip()
        if not holder:
            raise service.StrategyValidationError("lease_owner is required")
        if expected_lease_epoch < 0:
            raise service.StrategyValidationError("expected_lease_epoch must be >= 0")
        if not isinstance(expected_attempt, int) or isinstance(expected_attempt, bool) or expected_attempt < 1:
            raise service.StrategyValidationError("expected_attempt must be >= 1")
        if not isinstance(lease_until, datetime) or lease_until.tzinfo is None:
            raise service.StrategyValidationError("lease_until must be a timezone-aware datetime")
        if lease_until <= _utcnow():
            raise service.StrategyValidationError("lease_until must be in the future")

        session = self._session()
        try:
            # Lock the parent strategy row FIRST so a concurrent disable/update
            # serialises with this claim: a queued job cannot be claimed after
            # the strategy is disabled.
            strategy_id = self._strategy_id_for_job(session, job_id)
            if strategy_id is None:
                session.rollback()
                return None
            strategy = self._lock_strategy(session, strategy_id)
            if strategy is None:
                session.rollback()
                return None
            if strategy.status != "active":
                session.rollback()
                raise StrategyDisabled("strategy is disabled")

            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.status == "queued",
                    StrategyJob.desired_state == "started",
                    StrategyJob.lease_epoch == expected_lease_epoch,
                    StrategyJob.attempt == expected_attempt,
                    (StrategyJob.lease_until.is_(None)) | (StrategyJob.lease_until < _utcnow()),
                )
                .values(
                    lease_owner=holder,
                    lease_epoch=StrategyJob.lease_epoch + 1,
                    lease_until=lease_until,
                    status="starting",
                    updated_at=_utcnow(),
                )
            )
            if not result.rowcount:
                session.rollback()
                return None
            session.commit()
            return session.execute(
                select(StrategyJob).where(StrategyJob.id == job_id)
            ).scalar_one_or_none()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_recovery_required(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
    ) -> bool:
        """Durably fence a job to ``recovery_required``.

        Committed in its own transaction: a later failing effect must not roll the
        fence back. Requires id + lease_owner + epoch + attempt to match.
        """
        session = self._session()
        try:
            strategy_id = self._strategy_id_for_job(session, job_id)
            if strategy_id is None:
                session.rollback()
                return False
            self._lock_strategy(session, strategy_id)
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.lease_owner == lease_owner,
                    StrategyJob.lease_epoch == expected_lease_epoch,
                    StrategyJob.attempt == expected_attempt,
                    StrategyJob.status.notin_(("stopped", "failed", _UNRECONCILED)),
                )
                .values(
                    status=_UNRECONCILED,
                    recovery_required_at=_utcnow(),
                    lease_owner=None,
                    lease_until=None,
                    updated_at=_utcnow(),
                )
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def expire_to_recovery(self, job_id: str, *, expected_attempt: int) -> bool:
        """Trusted reconciler: fence an EXPIRED starting/running job to recovery.

        Does not pretend the expired worker is still authorized — it clears the
        lease and records ``recovery_required``. Committed in its own
        transaction. Applies only to a starting/running job whose lease has
        expired, and only at the expected attempt.
        """
        session = self._session()
        try:
            strategy_id = self._strategy_id_for_job(session, job_id)
            if strategy_id is None:
                session.rollback()
                return False
            self._lock_strategy(session, strategy_id)
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.attempt == expected_attempt,
                    StrategyJob.status.in_(("starting", "running")),
                    or_(
                        StrategyJob.lease_until.is_(None),
                        StrategyJob.lease_until < _utcnow(),
                    ),
                )
                .values(
                    status=_UNRECONCILED,
                    recovery_required_at=_utcnow(),
                    lease_owner=None,
                    lease_until=None,
                    updated_at=_utcnow(),
                )
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def reconcile_recovery(
        self,
        job_id: str,
        *,
        owner_id: str,
        expected_lease_epoch: int,
        expected_attempt: int,
    ) -> bool:
        """Explicit, owner-scoped reconciliation that clears the recovery block."""
        session = self._session()
        try:
            row = session.execute(
                select(StrategyJob.strategy_id, StrategyJob.owner_id).where(
                    StrategyJob.id == job_id
                )
            ).first()
            if row is None or str(row.owner_id) != owner_id:
                session.rollback()
                return False
            self._lock_strategy(session, str(row.strategy_id), owner_id)
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.owner_id == owner_id,
                    StrategyJob.status == _UNRECONCILED,
                    StrategyJob.lease_epoch == expected_lease_epoch,
                    StrategyJob.attempt == expected_attempt,
                )
                .values(status="stopped", reconciled_at=_utcnow(), updated_at=_utcnow())
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def has_unreconciled_recovery(self, owner_id: str, strategy_id: str) -> bool:
        session = self._session()
        try:
            row = session.execute(
                select(StrategyJob.id).where(
                    StrategyJob.owner_id == owner_id,
                    StrategyJob.strategy_id == strategy_id,
                    StrategyJob.status == _UNRECONCILED,
                    StrategyJob.reconciled_at.is_(None),
                )
            ).first()
            return row is not None
        finally:
            session.close()

    def assert_replacement_allowed(self, owner_id: str, strategy_id: str) -> None:
        """Refuse a replacement attempt while recovery is unreconciled.

        ``create_job`` enforces this in its own transaction; this helper exists
        for callers that only want the check (and for tests).
        """
        if self.has_unreconciled_recovery(owner_id, strategy_id):
            raise StrategyFenceError(
                "a previous attempt is in recovery_required; reconcile it before "
                "starting a new attempt"
            )

    # -- schedules (stored only; not exposed by this slice) -----------------

    def create_schedule(
        self,
        *,
        strategy_id: str,
        version_id: str,
        owner_id: str,
        job_kind: str,
        execution_mode: str,
        params: Optional[Dict[str, Any]] = None,
        schedule_kind: str = "daily",
        at_time: str,
        weekday: Optional[int] = None,
        timezone: str = "Asia/Kolkata",
        window_end: Optional[str] = None,
        squareoff_at: Optional[str] = None,
        enabled: bool = True,
    ) -> HostedStrategySchedule:
        """Store a schedule, validating identity and deriving snapshots."""
        schedule = service.validate_schedule(
            schedule_kind=schedule_kind,
            at_time=at_time,
            weekday=weekday,
            timezone=timezone,
            window_end=window_end,
            squareoff_at=squareoff_at,
        )
        if job_kind not in service.ALLOWED_JOB_KINDS:
            raise service.StrategyValidationError("unsupported job_kind")
        if execution_mode not in service.ALLOWED_EXECUTION_MODES:
            raise service.StrategyValidationError("unsupported execution_mode")

        session = self._session()
        try:
            strategy = self._lock_strategy(session, strategy_id, owner_id)
            if strategy is None:
                raise StrategyNotFound("strategy not found for this owner")
            if strategy.status != "active":
                raise StrategyDisabled("strategy is disabled")
            service.validate_account_scope(strategy.default_account_scope, execution_mode)
            version = session.execute(
                select(HostedStrategyVersion).where(
                    HostedStrategyVersion.id == version_id,
                    HostedStrategyVersion.strategy_id == strategy_id,
                )
            ).scalar_one_or_none()
            if version is None:
                raise StrategyIdentityError("version does not belong to this strategy")
            params_snapshot = service.validate_parameters(version.parameters_schema, params)
            policy_snapshot = service.build_policy_snapshot(
                stale_exit_policy=strategy.stale_exit_policy,
                max_duration_s=strategy.max_duration_s,
                progress_deadline_s=strategy.progress_deadline_s,
            )
            row = HostedStrategySchedule(
                id=service.new_schedule_id(),
                strategy_id=strategy_id,
                version_id=version_id,
                owner_id=owner_id,
                account_scope=strategy.default_account_scope,
                params_snapshot=copy.deepcopy(params_snapshot),
                execution_mode=execution_mode,
                job_kind=job_kind,
                policy_snapshot=copy.deepcopy(policy_snapshot),
                capabilities_snapshot=copy.deepcopy(dict(version.capabilities_snapshot or {})),
                max_duration_s=strategy.max_duration_s,
                progress_deadline_s=strategy.progress_deadline_s,
                schedule_kind=schedule["schedule_kind"],
                at_time=schedule["at_time"],
                weekday=schedule["weekday"],
                timezone=schedule["timezone"],
                window_end=schedule["window_end"],
                squareoff_at=schedule["squareoff_at"],
                enabled=bool(enabled),
            )
            session.add(row)
            session.commit()
            return row
        except IntegrityError as exc:
            session.rollback()
            raise StrategyConflict("this strategy already has a schedule") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_schedule(self, strategy_id: str) -> Optional[HostedStrategySchedule]:
        session = self._session()
        try:
            return session.execute(
                select(HostedStrategySchedule).where(
                    HostedStrategySchedule.strategy_id == strategy_id
                )
            ).scalar_one_or_none()
        finally:
            session.close()
