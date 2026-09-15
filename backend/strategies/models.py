"""ORM tables for the hosted-strategy foundation.

Defined on the shared ``Base`` from :mod:`backend.workflows.repository` so a
single ``Base.metadata.create_all`` (used throughout the test suite) registers
every platform table, and so the Postgres migration
``20260915_000019_hosted_strategy_foundation`` mirrors exactly these tables.

The ORM uses ``JSON`` (portable to the SQLite test database). The Postgres
migration and ``schema.sql`` use ``JSONB`` for the same columns — the established
pattern in this repo (see the alerts-platform repository).

Identity relations are **composite foreign keys**: a job/schedule can only point
at a version that belongs to its strategy and a strategy whose owner matches the
recorded owner. Nothing here is execution state.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
    func,
)

from backend.workflows.repository import Base


class HostedStrategy(Base):
    __tablename__ = "hosted_strategies"

    id = Column(Text, primary_key=True)
    #: The app owner. Server-derived (``app:<username>``); never actor-supplied.
    owner_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    #: ``hosted:<id>`` — the worker ``template_id`` this strategy maps to.
    template_id = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    default_execution_mode = Column(Text, nullable=False, default="paper")
    default_job_kind = Column(Text, nullable=False, default="finite")
    default_account_scope = Column(Text, nullable=False)
    #: Required explicitly until defaults are agreed (coordinator constraint):
    #: no DB default, so a strategy cannot be created without them.
    max_duration_s = Column(Integer, nullable=False)
    progress_deadline_s = Column(Integer, nullable=False)
    stale_exit_policy = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_hosted_strategies_owner_name"),
        UniqueConstraint("template_id", name="uq_hosted_strategies_template"),
        # Composite target for child FKs: (id, owner_id).
        UniqueConstraint("id", "owner_id", name="uq_hosted_strategies_id_owner"),
        CheckConstraint("template_id = 'hosted:' || id", name="ck_hosted_strategies_template_id"),
        CheckConstraint(
            "default_execution_mode IN ('paper', 'dry_run')",
            name="ck_hosted_strategies_execution_mode",
        ),
        CheckConstraint(
            "default_job_kind IN ('continuous', 'finite')",
            name="ck_hosted_strategies_job_kind",
        ),
        CheckConstraint("max_duration_s > 0", name="ck_hosted_strategies_max_duration"),
        CheckConstraint("progress_deadline_s > 0", name="ck_hosted_strategies_progress_deadline"),
        CheckConstraint(
            "stale_exit_policy IN ('none', 'exit_on_worker_stale')",
            name="ck_hosted_strategies_stale_policy",
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_hosted_strategies_status"),
        Index("idx_hosted_strategies_owner", "owner_id"),
    )


class HostedStrategyVersion(Base):
    """An immutable source revision.

    Immutability is enforced by the repository: there is no update/delete path,
    and ``(strategy_id, version)`` is UNIQUE so numbering cannot collide.
    """

    __tablename__ = "hosted_strategy_versions"

    id = Column(Text, primary_key=True)
    strategy_id = Column(
        Text, ForeignKey("hosted_strategies.id", ondelete="CASCADE"), nullable=False
    )
    version = Column(Integer, nullable=False)
    source = Column(Text, nullable=False)
    source_sha256 = Column(Text, nullable=False)
    parameters_schema = Column(JSON, nullable=False, default=dict)
    capabilities_snapshot = Column(JSON, nullable=False, default=dict)
    created_by = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_hosted_strategy_versions_number"),
        # Composite target for child FKs: (id, strategy_id).
        UniqueConstraint("id", "strategy_id", name="uq_hosted_strategy_versions_id_strategy"),
        CheckConstraint("version > 0", name="ck_hosted_strategy_versions_number"),
        Index("idx_hosted_strategy_versions_strategy", "strategy_id", "version"),
    )


class HostedStrategySchedule(Base):
    """Bounded schedule configuration. STORED only — not exposed by this slice."""

    __tablename__ = "hosted_strategy_schedules"

    id = Column(Text, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    version_id = Column(Text, nullable=False)
    owner_id = Column(Text, nullable=False)
    account_scope = Column(Text, nullable=False)
    params_snapshot = Column(JSON, nullable=False, default=dict)
    execution_mode = Column(Text, nullable=False)
    job_kind = Column(Text, nullable=False)
    policy_snapshot = Column(JSON, nullable=False, default=dict)
    capabilities_snapshot = Column(JSON, nullable=False, default=dict)
    max_duration_s = Column(Integer, nullable=False)
    progress_deadline_s = Column(Integer, nullable=False)
    schedule_kind = Column(Text, nullable=False)
    at_time = Column(Text, nullable=False)
    weekday = Column(Integer, nullable=True)
    timezone = Column(Text, nullable=False, default="Asia/Kolkata")
    window_end = Column(Text, nullable=True)
    squareoff_at = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    manual_paused_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("strategy_id", name="uq_hosted_strategy_schedules_strategy"),
        ForeignKeyConstraint(
            ["strategy_id", "owner_id"],
            ["hosted_strategies.id", "hosted_strategies.owner_id"],
            ondelete="CASCADE",
            name="fk_hosted_strategy_schedules_strategy_owner",
        ),
        ForeignKeyConstraint(
            ["version_id", "strategy_id"],
            ["hosted_strategy_versions.id", "hosted_strategy_versions.strategy_id"],
            ondelete="RESTRICT",
            name="fk_hosted_strategy_schedules_version_strategy",
        ),
        CheckConstraint(
            "execution_mode IN ('paper', 'dry_run')",
            name="ck_hosted_strategy_schedules_execution_mode",
        ),
        CheckConstraint(
            "job_kind IN ('continuous', 'finite')",
            name="ck_hosted_strategy_schedules_job_kind",
        ),
        CheckConstraint(
            "schedule_kind IN ('daily', 'weekly')",
            name="ck_hosted_strategy_schedules_kind",
        ),
        CheckConstraint(
            "weekday IS NULL OR (weekday >= 0 AND weekday <= 6)",
            name="ck_hosted_strategy_schedules_weekday",
        ),
        CheckConstraint(
            "schedule_kind <> 'weekly' OR weekday IS NOT NULL",
            name="ck_hosted_strategy_schedules_weekly_weekday",
        ),
        CheckConstraint("max_duration_s > 0", name="ck_hosted_strategy_schedules_max_duration"),
        CheckConstraint(
            "progress_deadline_s > 0", name="ck_hosted_strategy_schedules_progress_deadline"
        ),
    )


class StrategyJob(Base):
    """Durable job ledger with lease-epoch/attempt fencing.

    ``status='recovery_required'`` blocks a replacement attempt until an explicit
    reconciliation; ``lease_epoch`` + ``attempt`` fence authority-bearing
    transitions.
    """

    __tablename__ = "strategy_jobs"

    id = Column(Text, primary_key=True)
    strategy_id = Column(Text, nullable=False)
    version_id = Column(Text, nullable=False)
    owner_id = Column(Text, nullable=False)
    #: Pinned at creation from the strategy; never re-derived later.
    account_scope = Column(Text, nullable=False)
    job_kind = Column(Text, nullable=False)
    execution_mode = Column(Text, nullable=False)
    desired_state = Column(Text, nullable=False, default="started")
    occurrence_key = Column(Text, nullable=True)
    #: TEXT to match algo_worker_runs.strategy_run_id (TEXT), not UUID.
    run_id = Column(Text, nullable=True)
    token_id = Column(Text, nullable=True)
    lease_owner = Column(Text, nullable=True)
    lease_epoch = Column(BigInteger, nullable=False, default=0)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(Text, nullable=False, default="queued")
    #: Immutable snapshots — a queued job is never reconstructed from mutable
    #: strategy defaults.
    params_snapshot = Column(JSON, nullable=False, default=dict)
    capabilities_snapshot = Column(JSON, nullable=False, default=dict)
    policy_snapshot = Column(JSON, nullable=False, default=dict)
    max_duration_s = Column(Integer, nullable=False)
    progress_deadline_s = Column(Integer, nullable=False)
    identity_json = Column(JSON, nullable=False, default=dict)
    last_progress_at = Column(DateTime(timezone=True), nullable=True)
    exit_code = Column(Integer, nullable=True)
    log_ref = Column(Text, nullable=True)
    #: Set exactly once, when a supervise launch has delivered the child
    #: configuration (run id + session nonce + one-time token) to the
    #: supervisor. It is the durable marker that a credential handoff happened,
    #: so a repeated preparation fails closed instead of minting a second
    #: credential. ``NULL`` means the handoff is still uncertain (or never ran).
    handoff_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    recovery_required_at = Column(DateTime(timezone=True), nullable=True)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_strategy_jobs_occurrence"),
        ForeignKeyConstraint(
            ["strategy_id", "owner_id"],
            ["hosted_strategies.id", "hosted_strategies.owner_id"],
            ondelete="CASCADE",
            name="fk_strategy_jobs_strategy_owner",
        ),
        ForeignKeyConstraint(
            ["version_id", "strategy_id"],
            ["hosted_strategy_versions.id", "hosted_strategy_versions.strategy_id"],
            ondelete="RESTRICT",
            name="fk_strategy_jobs_version_strategy",
        ),
        CheckConstraint("job_kind IN ('continuous', 'finite')", name="ck_strategy_jobs_job_kind"),
        CheckConstraint(
            "execution_mode IN ('paper', 'dry_run')", name="ck_strategy_jobs_execution_mode"
        ),
        CheckConstraint(
            "desired_state IN ('started', 'paused', 'stopped')",
            name="ck_strategy_jobs_desired_state",
        ),
        CheckConstraint("attempt > 0", name="ck_strategy_jobs_attempt"),
        CheckConstraint("lease_epoch >= 0", name="ck_strategy_jobs_lease_epoch"),
        CheckConstraint("max_duration_s > 0", name="ck_strategy_jobs_max_duration"),
        CheckConstraint("progress_deadline_s > 0", name="ck_strategy_jobs_progress_deadline"),
        CheckConstraint(
            "status IN ('queued', 'starting', 'running', 'fencing', "
            "'recovery_required', 'stopped', 'failed', 'hung')",
            name="ck_strategy_jobs_status",
        ),
        Index("idx_strategy_jobs_lease", "status", "lease_until"),
        Index("idx_strategy_jobs_owner_strategy", "owner_id", "strategy_id"),
    )


__all__ = [
    "HostedStrategy",
    "HostedStrategySchedule",
    "HostedStrategyVersion",
    "StrategyJob",
]
