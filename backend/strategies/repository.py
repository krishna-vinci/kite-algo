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

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.strategies.attribution_models import (
    ExternalStrategyAdapter,
    Strategy,
    StrategyScheduleOccurrence,
)
from backend.strategies.models import (
    HostedStrategy,
    HostedStrategySchedule,
    HostedStrategyVersion,
    StrategyJob,
    StrategyJobLog,
    StrategyJobReconciliation,
)
from backend.strategies import service

__all__ = [
    "SqlAlchemyStrategyRepository",
    "StrategyConflict",
    "StrategyDisabled",
    "StrategyFenceError",
    "StrategyIdempotencyConflict",
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


class StrategyIdempotencyConflict(Exception):
    """An idempotency key was replayed with a different launch request."""


class StrategyDisabled(Exception):
    """A job may not be created or claimed for a disabled strategy.

    Disable is metadata only in this slice: it stops NEW attempts, it does not
    stop an already-running job (there is no runner to stop it).
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dialect_name(session: Any) -> str:
    """The session's dialect name (``postgresql``/``sqlite``), best effort.

    Used for the one thing that genuinely differs between the production database
    and the SQLite test fixture: ``NOW()`` versus ``CURRENT_TIMESTAMP``. An
    unknown session shape defaults to PostgreSQL, which is the production path.
    """
    bind = None
    getter = getattr(session, "get_bind", None)
    if callable(getter):
        try:
            bind = getter()
        except Exception:  # noqa: BLE001 - an unknown session shape is not fatal
            bind = None
    return str(getattr(getattr(bind, "dialect", None), "name", None) or "postgresql")


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
            # Canonical identity and the hosted adapter are written together: the
            # adapter is only a compute adapter over the canonical strategy, so a
            # partial write would be identity drift. The canonical row goes first
            # so the adapter's composite FK is satisfied on PostgreSQL.
            session.add(
                Strategy(
                    id=strategy_id,
                    owner_id=owner_id,
                    name=name,
                    account_scope=account_scope,
                    status="active",
                )
            )
            session.flush()
            session.add(row)
            session.commit()
            return row
        except IntegrityError as exc:
            session.rollback()
            raise StrategyConflict("a strategy with this name already exists") from exc
        finally:
            session.close()

    def get_canonical_strategy(self, owner_id: str, strategy_id: str) -> Optional[Strategy]:
        """The canonical product identity for one owner-scoped strategy."""
        session = self._session()
        try:
            return session.execute(
                select(Strategy).where(
                    Strategy.id == strategy_id,
                    Strategy.owner_id == owner_id,
                )
            ).scalar_one_or_none()
        finally:
            session.close()

    def list_product_statuses(self, owner_id: str) -> Dict[str, str]:
        """``{strategy_id: canonical product status}`` for one owner."""
        session = self._session()
        try:
            rows = session.execute(
                select(Strategy.id, Strategy.status).where(Strategy.owner_id == owner_id)
            ).all()
            return {str(row[0]): str(row[1]) for row in rows}
        finally:
            session.close()

    def adapter_kinds(self, owner_id: str) -> Dict[str, List[str]]:
        """``{strategy_id: [adapter kinds]}`` for one owner's strategies.

        A strategy is one product with one or more compute adapters; the UI needs
        to know which, not to infer it.
        """
        session = self._session()
        try:
            hosted = session.execute(
                select(HostedStrategy.id).where(HostedStrategy.owner_id == owner_id)
            ).scalars().all()
            external = session.execute(
                select(ExternalStrategyAdapter.strategy_id)
                .join(Strategy, Strategy.id == ExternalStrategyAdapter.strategy_id)
                .where(Strategy.owner_id == owner_id)
            ).scalars().all()
            kinds: Dict[str, List[str]] = {str(sid): [] for sid in hosted}
            for sid in hosted:
                kinds.setdefault(str(sid), []).append("hosted")
            for sid in external:
                kinds.setdefault(str(sid), []).append("external")
            return kinds
        finally:
            session.close()

    def set_product_status(
        self, owner_id: str, strategy_id: str, status: str
    ) -> Optional[Strategy]:
        """Set the canonical PRODUCT status (independent of scheduling)."""
        session = self._session()
        try:
            row = session.execute(
                select(Strategy).where(
                    Strategy.id == strategy_id, Strategy.owner_id == owner_id
                )
            ).scalar_one_or_none()
            if row is None:
                session.rollback()
                return None
            row.status = status
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_external_adapter(
        self,
        *,
        owner_id: str,
        strategy_id: str,
        config: Dict[str, Any],
        created_by: str,
    ) -> Optional[ExternalStrategyAdapter]:
        """Attach an external compute adapter to an owner's canonical strategy."""
        session = self._session()
        try:
            strategy = session.execute(
                select(Strategy).where(
                    Strategy.id == strategy_id, Strategy.owner_id == owner_id
                )
            ).scalar_one_or_none()
            if strategy is None:
                session.rollback()
                return None
            existing = session.execute(
                select(ExternalStrategyAdapter).where(
                    ExternalStrategyAdapter.strategy_id == strategy_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.config_json = dict(config)
                session.commit()
                return existing
            row = ExternalStrategyAdapter(
                id=f"ext_{strategy_id}",
                strategy_id=strategy_id,
                status="active",
                config_json=dict(config),
                created_by=created_by,
            )
            session.add(row)
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_external_strategy(
        self,
        *,
        owner_id: str,
        name: str,
        account_scope: str,
        description: Optional[str],
        config: Dict[str, Any],
        created_by: str,
    ) -> Strategy:
        """Create a canonical strategy with only an external compute adapter."""
        strategy_id = service.new_strategy_id()
        session = self._session()
        try:
            session.add(
                Strategy(
                    id=strategy_id,
                    owner_id=owner_id,
                    name=name,
                    account_scope=account_scope,
                    status="active",
                )
            )
            session.flush()
            session.add(
                ExternalStrategyAdapter(
                    id=f"ext_{strategy_id}",
                    strategy_id=strategy_id,
                    status="active",
                    config_json=dict(config),
                    created_by=created_by,
                )
            )
            session.commit()
            return session.execute(
                select(Strategy).where(Strategy.id == strategy_id)
            ).scalar_one()
        except IntegrityError as exc:
            session.rollback()
            raise StrategyConflict("a strategy with this name already exists") from exc
        except Exception:
            session.rollback()
            raise
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
        name: Any = _UNSET,
    ) -> Optional[HostedStrategy]:
        """Minimal owner-scoped metadata update. Versions are never touched.

        Returns ``None`` for a foreign/missing id (router renders 404).

        A rename writes the canonical product name and the hosted compatibility
        mirror in the **same transaction**, so the two can never drift.
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
            if name is not _UNSET:
                canonical = session.execute(
                    select(Strategy).where(Strategy.id == strategy_id)
                ).scalar_one_or_none()
                if canonical is None:
                    # A hosted adapter without canonical identity is drift, which
                    # is refused rather than papered over.
                    raise StrategyIdentityError(
                        "hosted strategy has no canonical identity"
                    )
                row.name = name
                canonical.name = name
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
        identity: Optional[Dict[str, Any]] = None,
        attempt: int = 1,
        desired_state: str = "started",
        session: Optional[Session] = None,
    ) -> StrategyJob:
        """Create a queued job, enforcing identity, snapshots and the block.

        The whole check + insert runs in one transaction that first locks the
        strategy row, so a concurrent recovery/reconciliation serialises with it
        and a race cannot bypass the block.

        ``identity`` is the launch's *bound evaluation identity* (for a schedule
        occurrence: the occurrence key, the evaluation id the child must submit
        under, and the due time). It is stored on the job and is part of what an
        idempotent replay must match, so a retry under the same key can never
        silently rebind the launch to a different evaluation.

        ``session`` lets a caller compose the insert into **its own transaction**
        (the scheduler's atomic job+occurrence decision). With a caller session
        this method never commits, closes or rolls back: the caller owns the
        transaction, so it can commit the job row and its occurrence decision
        together or roll both back. Errors still surface as the typed
        ``Strategy*`` exceptions.
        """
        if job_kind not in service.ALLOWED_JOB_KINDS:
            raise service.StrategyValidationError("unsupported job_kind")
        if execution_mode not in service.ALLOWED_EXECUTION_MODES:
            raise service.StrategyValidationError("unsupported execution_mode")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise service.StrategyValidationError("attempt must be an integer >= 1")

        caller_session = session is not None
        session = session if session is not None else self._session()
        try:
            strategy = self._lock_strategy(session, strategy_id, owner_id)
            if strategy is None:
                raise StrategyNotFound("strategy not found for this owner")
            # Idempotent replay: the occurrence_key is unique and the strategy row
            # is locked, so a retry returns the original job instead of creating a
            # duplicate (and is not subject to the active-job block).
            if occurrence_key:
                existing = session.execute(
                    select(StrategyJob).where(StrategyJob.occurrence_key == occurrence_key)
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.owner_id != owner_id or existing.strategy_id != strategy_id:
                        raise StrategyConflict("occurrence_key already used by another job")
                    # Bind the key to the normalized launch request: an identical
                    # replay returns the original job; a different request is a
                    # documented conflict (never a silent reuse or duplicate).
                    replay_version = session.execute(
                        select(HostedStrategyVersion).where(
                            HostedStrategyVersion.id == version_id,
                            HostedStrategyVersion.strategy_id == strategy_id,
                        )
                    ).scalar_one_or_none()
                    if replay_version is None:
                        raise StrategyIdentityError("version does not belong to this strategy")
                    normalized_params = service.validate_parameters(
                        replay_version.parameters_schema, params
                    )
                    if (
                        str(existing.version_id) != str(version_id)
                        or str(existing.execution_mode) != str(execution_mode)
                        or str(existing.job_kind) != str(job_kind)
                        or dict(existing.params_snapshot or {}) != normalized_params
                        or dict(existing.identity_json or {}) != dict(identity or {})
                    ):
                        raise StrategyIdempotencyConflict(
                            "the idempotency key was already used for a different launch request"
                        )
                    return existing
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
                identity_json=copy.deepcopy(dict(identity or {})),
            )
            session.add(row)
            if caller_session:
                session.flush()
            else:
                session.commit()
            return row
        except IntegrityError as exc:
            if not caller_session:
                session.rollback()
            raise StrategyConflict("occurrence_key already exists") from exc
        except Exception:
            if not caller_session:
                session.rollback()
            raise
        finally:
            if not caller_session:
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

    def list_jobs_for_strategy(
        self, owner_id: str, strategy_id: str, *, limit: int = 50
    ) -> List[StrategyJob]:
        capped = max(1, min(int(limit), 200))
        session = self._session()
        try:
            return list(
                session.execute(
                    select(StrategyJob)
                    .where(
                        StrategyJob.owner_id == owner_id,
                        StrategyJob.strategy_id == strategy_id,
                    )
                    .order_by(StrategyJob.created_at.desc(), StrategyJob.id.desc())
                    .limit(capped)
                ).scalars()
            )
        finally:
            session.close()

    # -- reconciliation evidence + audit ------------------------------------

    def report_process_cleanup(
        self,
        job_id: str,
        *,
        state: str,
        actor: str,
        expected_attempt: int,
    ) -> bool:
        """Record supervisor-reported process cleanup, bound to the attempt.

        Only the supervisor lifecycle API calls this; the update matches the
        immutable ``attempt`` so a report can never apply to a different attempt.
        """
        if state not in ("confirmed", "unresolved"):
            raise service.StrategyValidationError("process cleanup state must be confirmed/unresolved")
        session = self._session()
        try:
            # Lock the parent strategy row so a concurrent reconciliation
            # (which also locks it) sees a stable cleanup state.
            strategy_id = self._strategy_id_for_job(session, job_id)
            if strategy_id is not None:
                self._lock_strategy(session, strategy_id)
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.attempt == expected_attempt,
                    StrategyJob.status.notin_(("queued",)),
                )
                .values(
                    process_cleanup_state=state,
                    process_cleanup_at=_utcnow(),
                    process_cleanup_actor=str(actor)[:200],
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

    def reconcile_with_audit(
        self,
        job_id: str,
        *,
        owner_id: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        expected_process_cleanup_state: Optional[str],
        expected_run_id: Optional[str],
        reason_code: str,
        evidence: Dict[str, Any],
        actor_id: str,
        settlement_barrier: Any = None,
        barrier_account_id: Optional[str] = None,
        barrier_strategy_id: Optional[str] = None,
        barrier_environment: Optional[str] = None,
        expected_barrier_version: Optional[int] = None,
        require_barrier_proof: bool = False,
        close_worker_run: bool = False,
        worker_run_id: Optional[str] = None,
    ) -> Optional[StrategyJobReconciliation]:
        """Clear the replacement block **and** append the audit row atomically.

        One transaction: locks the strategy row (serializing with ``create_job``
        and with cleanup-state updates), CAS-matches the in-DB evidence
        (owner/attempt/lease-epoch/recovery state/process-cleanup/run), updates the
        job to ``stopped`` and inserts the audit row. If the CAS loses, or the
        audit insert fails, the whole transaction rolls back so the block is never
        cleared without a durable record. Returns the audit row or ``None``.

        When ``require_barrier_proof`` is set the SAME transaction also validates
        the execution-settlement proof for the exact book (``barrier_*``): it takes
        that book's advisory lock - the lock the work events take, so a concurrent
        writer serializes rather than racing - and requires a CURRENT proof at
        ``expected_barrier_version``. A missing, stale or wrong-book proof refuses
        (returns ``None``) instead of unblocking on evidence that two reads agreed
        on. Lock order is strategy row -> book (the order every writer uses); no
        path takes the book lock first and then a strategy row, so it cannot
        invert.

        When ``close_worker_run`` is set the SAME transaction also closes the
        linked hosted worker run (``worker_run_id``, the job's ``run_id``) with
        its existing closed-status semantics - ``status='closed'`` plus a
        ``closed_at`` stamp - so the unblock and the run's terminal state commit
        together. If the run cannot be closed the whole transaction rolls back
        and the attempt stays blocked: replacement is never unblocked while the
        linked trading run is still open. Data-only/unlaunched attempts leave
        this off and are unaffected.
        """
        session = self._session()
        try:
            row = session.execute(
                select(StrategyJob.strategy_id, StrategyJob.owner_id).where(StrategyJob.id == job_id)
            ).first()
            if row is None or str(row.owner_id) != owner_id:
                session.rollback()
                return None
            strategy_id = str(row.strategy_id)
            self._lock_strategy(session, strategy_id, owner_id)

            if require_barrier_proof:
                if settlement_barrier is None or expected_barrier_version is None:
                    # No barrier, or no exact proof version: nothing can be
                    # validated, so the invariant refuses (never "any book will do").
                    session.rollback()
                    return None
                # The book is DERIVED from the persisted job inside this
                # transaction - caller-supplied coordinates are ignored, so a
                # VALID proof for some other book can never unblock this attempt.
                job_row = session.execute(
                    select(StrategyJob.account_scope, StrategyJob.strategy_id, StrategyJob.execution_mode).where(
                        StrategyJob.id == job_id
                    )
                ).first()
                if job_row is None:
                    session.rollback()
                    return None
                book_account = str(job_row.account_scope or "")
                book_strategy = str(job_row.strategy_id or "")
                book_environment = str(job_row.execution_mode or "")
                settlement_barrier.lock_book(
                    session,
                    account_id=book_account,
                    strategy_id=book_strategy,
                    execution_environment=book_environment,
                )
                book_state = settlement_barrier.state(
                    account_id=book_account,
                    strategy_id=book_strategy,
                    execution_environment=book_environment,
                    db=session,
                )
                current_version = int(book_state.get("barrier_version") or 0)
                if not bool(book_state.get("proof_valid")) or current_version != int(
                    expected_barrier_version
                ):
                    session.rollback()
                    return None


            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.owner_id == owner_id,
                    StrategyJob.attempt == expected_attempt,
                    StrategyJob.lease_epoch == expected_lease_epoch,
                    StrategyJob.status == _UNRECONCILED,
                    StrategyJob.reconciled_at.is_(None),
                    StrategyJob.process_cleanup_state.is_not_distinct_from(
                        expected_process_cleanup_state
                    ),
                    StrategyJob.run_id.is_not_distinct_from(expected_run_id),
                )
                .values(status="stopped", reconciled_at=_utcnow(), updated_at=_utcnow())
            )
            if not result.rowcount:
                session.rollback()
                return None

            if close_worker_run:
                if not worker_run_id:
                    # A linked close was requested with no run named: refuse
                    # rather than unblock on an unverified closure.
                    session.rollback()
                    return None
                now_expr = (
                    "CURRENT_TIMESTAMP"
                    if _dialect_name(session) == "sqlite"
                    else "NOW()"
                )
                closed = session.execute(
                    text(
                        f"""
                        UPDATE public.algo_worker_runs
                        SET status = 'closed',
                            closed_at = COALESCE(closed_at, {now_expr}),
                            updated_at = {now_expr}
                        WHERE strategy_run_id = :strategy_run_id
                        """
                    ),
                    {"strategy_run_id": str(worker_run_id)},
                )
                if not int(getattr(closed, "rowcount", 0) or 0):
                    session.rollback()
                    return None

            audit_evidence = copy.deepcopy(dict(evidence or {}))
            if close_worker_run:
                audit_evidence["linked_worker_run_closed"] = str(worker_run_id)
            audit = StrategyJobReconciliation(
                id=service.new_reconciliation_id(),
                job_id=job_id,
                strategy_id=strategy_id,
                owner_id=owner_id,
                attempt=int(expected_attempt),
                run_id=expected_run_id,
                outcome="reconciled",
                reason_code=reason_code,
                evidence_json=audit_evidence,
                actor_id=actor_id,
            )
            session.add(audit)
            session.commit()
            return audit
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_reconciliation(
        self,
        *,
        job_id: str,
        strategy_id: str,
        owner_id: str,
        attempt: int,
        run_id: Optional[str],
        outcome: str,
        reason_code: str,
        evidence: Dict[str, Any],
        actor_id: str,
    ) -> StrategyJobReconciliation:
        """Append an audit row. Never updates or overwrites prior history."""
        session = self._session()
        try:
            row = StrategyJobReconciliation(
                id=service.new_reconciliation_id(),
                job_id=job_id,
                strategy_id=strategy_id,
                owner_id=owner_id,
                attempt=int(attempt),
                run_id=run_id,
                outcome=outcome,
                reason_code=reason_code,
                evidence_json=copy.deepcopy(dict(evidence or {})),
                actor_id=actor_id,
            )
            session.add(row)
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_reconciliations(
        self, job_id: str, *, limit: int = 50
    ) -> List[StrategyJobReconciliation]:
        capped = max(1, min(int(limit), 200))
        session = self._session()
        try:
            return list(
                session.execute(
                    select(StrategyJobReconciliation)
                    .where(StrategyJobReconciliation.job_id == job_id)
                    .order_by(
                        StrategyJobReconciliation.created_at.desc(),
                        StrategyJobReconciliation.id.desc(),
                    )
                    .limit(capped)
                ).scalars()
            )
        finally:
            session.close()

    # -- internal / supervisor-scoped lookups -------------------------------
    #
    # These are used by the narrow, credential-authenticated lifecycle API. They
    # are NOT owner-scoped (the supervisor is not an app user); the router
    # authorizes against the job's persisted lease/attempt authority instead, and
    # never lets a bare run id select an unrelated run.

    def get_job_by_id(self, job_id: str) -> Optional[StrategyJob]:
        session = self._session()
        try:
            return session.execute(
                select(StrategyJob).where(StrategyJob.id == job_id)
            ).scalar_one_or_none()
        finally:
            session.close()

    def list_jobs_by_status(self, statuses: tuple, *, limit: int = 50) -> List[StrategyJob]:
        """Narrow, unscoped listing for supervisor discovery.

        Returns the oldest jobs in the given statuses so a supervisor can find
        work. It carries no owner scoping because the lifecycle API already
        authenticates the narrow supervisor credential; it is still read-only and
        bounded.
        """
        capped = max(1, min(int(limit), 200))
        session = self._session()
        try:
            return list(
                session.execute(
                    select(StrategyJob)
                    .where(StrategyJob.status.in_(tuple(statuses)), StrategyJob.desired_state == "started")
                    .order_by(StrategyJob.created_at, StrategyJob.id)
                    .limit(capped)
                ).scalars()
            )
        finally:
            session.close()

    def get_job_by_run_id(self, run_id: str) -> Optional[StrategyJob]:
        if not run_id:
            return None
        session = self._session()
        try:
            return session.execute(
                select(StrategyJob).where(StrategyJob.run_id == run_id)
            ).scalars().first()
        finally:
            session.close()

    def get_job_by_token_id(self, token_id: str) -> Optional[StrategyJob]:
        if not token_id:
            return None
        session = self._session()
        try:
            return session.execute(
                select(StrategyJob).where(StrategyJob.token_id == token_id)
            ).scalars().first()
        finally:
            session.close()

    # -- launch preparation markers (fenced, one-way) ------------------------
    #
    # Each marker is recorded durably with an authority CAS, so a crash between
    # steps is visible on retry and preparation can fail closed instead of
    # re-minting a credential. See ``backend.api.services.hosted_lifecycle``.

    @staticmethod
    def _authority_clause(
        job_id: str, lease_owner: str, expected_lease_epoch: int, expected_attempt: int
    ):
        return and_(
            StrategyJob.id == job_id,
            StrategyJob.status.in_(("starting", "running")),
            StrategyJob.lease_owner == lease_owner,
            StrategyJob.lease_epoch == expected_lease_epoch,
            StrategyJob.attempt == expected_attempt,
        )

    def reserve_child_token(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        token_id: str,
    ) -> bool:
        """CAS the child ``token_id`` onto the job. Only one caller can win."""
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    self._authority_clause(
                        job_id, lease_owner, expected_lease_epoch, expected_attempt
                    ),
                    StrategyJob.token_id.is_(None),
                    StrategyJob.handoff_at.is_(None),
                )
                .values(token_id=token_id, updated_at=_utcnow())
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_child_run(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        token_id: str,
        run_id: str,
    ) -> bool:
        """CAS the child ``run_id`` onto the job, pinning the token it bounds."""
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    self._authority_clause(
                        job_id, lease_owner, expected_lease_epoch, expected_attempt
                    ),
                    StrategyJob.token_id == token_id,
                    StrategyJob.run_id.is_(None),
                    StrategyJob.handoff_at.is_(None),
                )
                .values(run_id=run_id, updated_at=_utcnow())
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_running_and_handoff(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        run_id: str,
    ) -> bool:
        """Record the successful handoff: status ``running`` + ``handoff_at``.

        One-way and authority-fenced. A second call is a no-op (``handoff_at``
        already set), so it cannot re-open a delivered attempt.
        """
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    self._authority_clause(
                        job_id, lease_owner, expected_lease_epoch, expected_attempt
                    ),
                    StrategyJob.run_id == run_id,
                    StrategyJob.handoff_at.is_(None),
                )
                .values(
                    status="running",
                    handoff_at=_utcnow(),
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

    def renew_lease(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        lease_until: datetime,
    ) -> bool:
        """Extend a live lease. Authority-fenced; does NOT touch progress.

        Runner liveness (this heartbeat) is deliberately distinct from strategy
        progress (``last_progress_at``), which is never written here.
        """
        if not isinstance(lease_until, datetime) or lease_until.tzinfo is None:
            raise service.StrategyValidationError("lease_until must be a timezone-aware datetime")
        if lease_until <= _utcnow():
            raise service.StrategyValidationError("lease_until must be in the future")
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    self._authority_clause(
                        job_id, lease_owner, expected_lease_epoch, expected_attempt
                    ),
                    StrategyJob.handoff_at.is_not(None),
                )
                .values(lease_until=lease_until, updated_at=_utcnow())
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_progress(self, job_id: str) -> bool:
        """Record a child-reported progress marker on a live job.

        Only the child-authenticated progress route calls this; the runner
        heartbeat never does. Grepping the schema, ``last_progress_at`` is the
        single progress signal and is written only here.
        """
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.status.in_(("starting", "running")),
                )
                .values(last_progress_at=_utcnow(), updated_at=_utcnow())
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_failure(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
        reason: str,
    ) -> bool:
        """Record a short, non-secret failure diagnostic under authority."""
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    self._authority_clause(
                        job_id, lease_owner, expected_lease_epoch, expected_attempt
                    )
                )
                .values(last_error=str(reason)[:500], updated_at=_utcnow())
            )
            session.commit()
            return bool(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_stopped(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
    ) -> bool:
        """Runner-owned stop under authority. Ends the live lease.

        A stop is NOT a claim of cancellation, flatness, or exposure
        reconciliation. ``lease_owner``/``lease_epoch``/``attempt`` are retained
        as **attribution** so an authorized state read still works after the
        transition; mutation authority is withdrawn by clearing ``lease_until``
        and moving the status out of the live set, not by erasing identity.
        """
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    self._authority_clause(
                        job_id, lease_owner, expected_lease_epoch, expected_attempt
                    )
                )
                .values(
                    status="stopped",
                    desired_state="stopped",
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
        ``lease_owner``/``lease_epoch``/``attempt`` are retained as attribution so
        an authorized state read still works after fencing; the live lease is
        ended by clearing ``lease_until`` and moving the status out of the live
        set, which is what withdraws mutation authority.
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

    def expire_to_recovery_authorized(
        self,
        job_id: str,
        *,
        lease_owner: str,
        expected_lease_epoch: int,
        expected_attempt: int,
    ) -> bool:
        """Authenticated recovery fence for an EXPIRED attempt.

        This is the lease-loss path: the ordinary fence requires a *live* lease,
        so an attempt whose lease has expired needs a distinct transition. It
        requires the full authority (id + ``lease_owner`` + epoch + attempt) so a
        stale or unrelated holder is refused, applies only to a ``starting``/
        ``running`` job whose lease has expired, and **cannot renew or regain**
        execution authority — it only ends the attempt durably as
        ``recovery_required``. Committed in its own transaction.
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
                    StrategyJob.status.in_(("starting", "running")),
                    or_(
                        StrategyJob.lease_until.is_(None),
                        StrategyJob.lease_until < _utcnow(),
                    ),
                )
                .values(
                    status=_UNRECONCILED,
                    recovery_required_at=_utcnow(),
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
        day_of_month: Optional[int] = None,
        calendar_dates: Optional[List[str]] = None,
        timezone: str = "Asia/Kolkata",
        window_end: Optional[str] = None,
        squareoff_at: Optional[str] = None,
        enabled: bool = True,
    ) -> HostedStrategySchedule:
        """Store a schedule, validating identity and deriving snapshots.

        Every supported kind is created here: a monthly schedule keeps its day of
        month and a calendar schedule its explicit dates, so the scheduler can
        materialise the same occurrences the operator configured.
        """
        schedule = service.validate_schedule(
            schedule_kind=schedule_kind,
            at_time=at_time,
            weekday=weekday,
            day_of_month=day_of_month,
            calendar_dates=calendar_dates,
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
                day_of_month=schedule["day_of_month"],
                calendar_dates=(
                    copy.deepcopy(schedule["calendar_dates"])
                    if schedule["calendar_dates"] is not None
                    else None
                ),
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

    def save_schedule(
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
        day_of_month: Optional[int] = None,
        calendar_dates: Optional[List[str]] = None,
        timezone: str = "Asia/Kolkata",
        window_end: Optional[str] = None,
        squareoff_at: Optional[str] = None,
        enabled: bool = True,
    ) -> "HostedStrategySchedule":
        """Create or edit this strategy's single stored schedule.

        The same validation and identity derivation as :meth:`create_schedule`,
        but an existing row is updated in place: its ``id`` is stable, so the
        occurrence history and the scheduler's ``UNIQUE (schedule_id,
        occurrence_key)`` fence stay bound to the same schedule instead of a
        re-created one. A disabled strategy cannot (re-)enable its schedule,
        because the scheduler's launch path would refuse the pinned job anyway;
        the stored schedule never silently resumes work.
        """
        schedule = service.validate_schedule(
            schedule_kind=schedule_kind,
            at_time=at_time,
            weekday=weekday,
            day_of_month=day_of_month,
            calendar_dates=calendar_dates,
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
            if enabled and strategy.status != "active":
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

            row = session.execute(
                select(HostedStrategySchedule).where(
                    HostedStrategySchedule.strategy_id == strategy_id
                )
            ).scalar_one_or_none()
            if row is None:
                row = HostedStrategySchedule(id=service.new_schedule_id(), strategy_id=strategy_id)
                session.add(row)
            # Every pinned field is rewritten: an edit is a re-pin, never a
            # partial update that leaves one snapshot from the previous config.
            row.version_id = version_id
            row.owner_id = owner_id
            row.account_scope = str(strategy.default_account_scope)
            row.params_snapshot = copy.deepcopy(params_snapshot)
            row.execution_mode = execution_mode
            row.job_kind = job_kind
            row.policy_snapshot = copy.deepcopy(policy_snapshot)
            row.capabilities_snapshot = copy.deepcopy(dict(version.capabilities_snapshot or {}))
            row.max_duration_s = int(strategy.max_duration_s)
            row.progress_deadline_s = int(strategy.progress_deadline_s)
            row.schedule_kind = schedule["schedule_kind"]
            row.at_time = schedule["at_time"]
            row.weekday = schedule["weekday"]
            row.day_of_month = schedule["day_of_month"]
            row.calendar_dates = (
                copy.deepcopy(schedule["calendar_dates"])
                if schedule["calendar_dates"] is not None
                else None
            )
            row.timezone = schedule["timezone"]
            row.window_end = schedule["window_end"]
            row.squareoff_at = schedule["squareoff_at"]
            row.enabled = bool(enabled)
            if row.enabled:
                row.manual_paused_at = None
            row.updated_at = _utcnow()
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_schedule_enabled(
        self, strategy_id: str, *, owner_id: str, enabled: bool, actor: Optional[str] = None
    ) -> "HostedStrategySchedule":
        """Enable or disable the strategy's stored schedule (idempotent).

        Enabling refuses when the strategy itself is disabled: the scheduler
        would only materialise work the launch path refuses, so the schedule
        must not be presented as resumed.
        """
        session = self._session()
        try:
            strategy = self._lock_strategy(session, strategy_id, owner_id)
            if strategy is None:
                raise StrategyNotFound("strategy not found for this owner")
            row = session.execute(
                select(HostedStrategySchedule).where(
                    HostedStrategySchedule.strategy_id == strategy_id
                )
            ).scalar_one_or_none()
            if row is None:
                raise StrategyNotFound("schedule not found")
            if enabled and strategy.status != "active":
                raise StrategyDisabled("strategy is disabled")
            row.enabled = bool(enabled)
            if enabled:
                row.manual_paused_at = None
            row.updated_at = _utcnow()
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_schedule_occurrences(
        self, schedule_id: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Materialised occurrences, newest due time first.

        Read-only: a missed run is a row the scheduler already wrote
        (``skipped``/``expired`` with its reason), so the operator view reports
        what happened instead of re-deriving it.
        """
        session = self._session()
        try:
            rows = session.execute(
                select(StrategyScheduleOccurrence)
                .where(StrategyScheduleOccurrence.schedule_id == str(schedule_id))
                .order_by(StrategyScheduleOccurrence.due_at.desc())
                .limit(int(limit))
            ).scalars().all()
        finally:
            session.close()
        return [
            {
                "occurrence_key": str(row.occurrence_key),
                "due_at": row.due_at.isoformat() if row.due_at else None,
                "status": str(row.status),
                "fired_at": row.fired_at.isoformat() if row.fired_at else None,
                "evaluation_id": row.evaluation_id,
                "skip_reason": row.skip_reason,
                "detail": dict(row.detail or {}),
            }
            for row in rows
        ]

    # -- operator controls: run-now idempotency, stop, bounded logs ---------

    def get_job_by_occurrence_key(self, occurrence_key: str) -> Optional[StrategyJob]:
        if not occurrence_key:
            return None
        session = self._session()
        try:
            return session.execute(
                select(StrategyJob).where(StrategyJob.occurrence_key == occurrence_key)
            ).scalar_one_or_none()
        finally:
            session.close()

    def stop_queued_job(
        self, job_id: str, *, owner_id: str, expected_attempt: int, actor: str
    ) -> bool:
        """Stop a queued job without launching it. Owner/attempt bound."""
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.owner_id == owner_id,
                    StrategyJob.attempt == expected_attempt,
                    StrategyJob.status == "queued",
                )
                .values(
                    status="stopped",
                    desired_state="stopped",
                    stop_requested_at=_utcnow(),
                    stop_requested_by=str(actor)[:200],
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

    def request_stop_active(
        self, job_id: str, *, owner_id: str, expected_attempt: int, actor: str
    ) -> bool:
        """Record a durable stop request for an active job.

        Only ``desired_state`` (and the stop attribution) changes — status and
        lease are untouched so the supervisor keeps authority to observe the
        request, perform a bounded cleanup and complete its authorized terminal
        transition. Idempotent: a repeat while already stopped is a no-op.
        """
        session = self._session()
        try:
            result = session.execute(
                update(StrategyJob)
                .where(
                    StrategyJob.id == job_id,
                    StrategyJob.owner_id == owner_id,
                    StrategyJob.attempt == expected_attempt,
                    StrategyJob.status.in_(("starting", "running")),
                    StrategyJob.desired_state != "stopped",
                )
                .values(
                    desired_state="stopped",
                    stop_requested_at=_utcnow(),
                    stop_requested_by=str(actor)[:200],
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

    def append_job_log(
        self,
        job_id: str,
        *,
        attempt: int,
        chunks: List[str],
        max_total_bytes: int,
    ) -> Dict[str, Any]:
        """Append bounded, already-redacted log chunks under the total cap.

        Byte accounting is exact (``byte_len`` in UTF-8 bytes) and the job row is
        locked so concurrent ingestion/retries cannot corrupt sequence allocation
        or over-consume the cap. Once the cap is reached, ``discarded`` is
        returned and persisted on the job so browser truncation reflects real
        loss. Returns ``{"stored", "truncated", "discarded", "next_seq"}``.
        """
        cap = max(1, int(max_total_bytes))
        for attempt_no in (1, 2):
            session = self._session()
            try:
                # Serialize ingestion per job (Postgres row lock; SQLite writes
                # already serialize). Prevents duplicate seq / cap overrun.
                session.execute(
                    select(StrategyJob.id).where(StrategyJob.id == job_id).with_for_update()
                ).first()
                current_bytes = int(
                    session.execute(
                        select(func.coalesce(func.sum(StrategyJobLog.byte_len), 0)).where(
                            StrategyJobLog.job_id == job_id,
                            StrategyJobLog.attempt == attempt,
                        )
                    ).scalar_one()
                )
                max_seq = int(
                    session.execute(
                        select(func.coalesce(func.max(StrategyJobLog.seq), 0)).where(
                            StrategyJobLog.job_id == job_id,
                            StrategyJobLog.attempt == attempt,
                        )
                    ).scalar_one()
                    or 0
                )
                next_seq = max_seq
                stored = 0
                discarded = False
                for chunk in chunks:
                    text = str(chunk or "")
                    if not text:
                        continue
                    size = len(text.encode("utf-8"))
                    if current_bytes + size > cap:
                        discarded = True
                        break
                    next_seq += 1
                    session.add(
                        StrategyJobLog(
                            job_id=job_id,
                            attempt=int(attempt),
                            seq=next_seq,
                            content=text,
                            byte_len=size,
                        )
                    )
                    current_bytes += size
                    stored += 1
                job = session.get(StrategyJob, job_id)
                if job is not None:
                    if job.logs_source is None:
                        job.logs_source = "post_termination"
                    if discarded:
                        job.logs_discarded = True
                session.commit()
                return {"stored": stored, "truncated": discarded, "discarded": discarded, "next_seq": next_seq}
            except IntegrityError:
                session.rollback()
                if attempt_no == 2:
                    raise
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        raise RuntimeError("unreachable")

    def list_job_logs(
        self, job_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> List[StrategyJobLog]:
        capped = max(1, min(int(limit), 500))
        session = self._session()
        try:
            return list(
                session.execute(
                    select(StrategyJobLog)
                    .where(StrategyJobLog.job_id == job_id, StrategyJobLog.seq > int(after_seq))
                    .order_by(StrategyJobLog.seq.asc())
                    .limit(capped)
                ).scalars()
            )
        finally:
            session.close()

    def job_log_byte_count(self, job_id: str) -> int:
        session = self._session()
        try:
            return int(
                session.execute(
                    select(func.coalesce(func.sum(StrategyJobLog.byte_len), 0)).where(
                        StrategyJobLog.job_id == job_id
                    )
                ).scalar_one()
            )
        finally:
            session.close()
