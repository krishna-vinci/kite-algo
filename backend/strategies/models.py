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
    text,
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
    #: How this strategy's execution requests are authorised. ``approval_based``
    #: is the default, and it is the only value a pre-existing strategy can be
    #: read as: an ``autonomous`` *selection* authorises nothing on its own, it
    #: only makes an owner-issued grant usable.
    authorization_mode = Column(
        Text, nullable=False, default="approval_based", server_default="approval_based"
    )
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
        # ``live`` is representable because migration 20260922_000039 widened the
        # database constraint; this ORM mirror is kept in step so a
        # ``create_all`` test database accepts the same vocabulary.
        CheckConstraint(
            "default_execution_mode IN ('paper', 'dry_run', 'live')",
            name="ck_hosted_strategies_execution_mode",
        ),
        CheckConstraint(
            "authorization_mode IN ('approval_based', 'autonomous')",
            name="ck_hosted_strategies_authorization_mode",
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
    #: The risk policy this immutable version declares (B2.5). ``NULL`` means the
    #: version declares none, which the options lane refuses by name.
    risk_policy = Column(JSON, nullable=True)
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
    #: Market-session kind: minutes after open when the job starts and before
    #: close when the platform asks its one session-long job to stop.
    start_offset_min = Column(Integer, nullable=True)
    stop_offset_min = Column(Integer, nullable=True)
    weekday = Column(Integer, nullable=True)
    #: Monthly kind: the day of month the occurrence falls on.
    day_of_month = Column(Integer, nullable=True)
    #: Calendar kind: the explicit dates, as ISO date strings.
    calendar_dates = Column(JSON, nullable=True)
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
            "execution_mode IN ('paper', 'dry_run', 'live')",
            name="ck_hosted_strategy_schedules_execution_mode",
        ),
        CheckConstraint(
            "job_kind IN ('continuous', 'finite')",
            name="ck_hosted_strategy_schedules_job_kind",
        ),
        CheckConstraint(
            "schedule_kind IN ('daily', 'weekly', 'monthly', 'calendar', 'market_session')",
            name="ck_hosted_strategy_schedules_kind",
        ),
        CheckConstraint(
            "start_offset_min IS NULL OR (start_offset_min >= 0 AND start_offset_min < 1440)",
            name="ck_hosted_strategy_schedules_start_offset",
        ),
        CheckConstraint(
            "stop_offset_min IS NULL OR (stop_offset_min >= 0 AND stop_offset_min < 1440)",
            name="ck_hosted_strategy_schedules_stop_offset",
        ),
        CheckConstraint(
            "schedule_kind <> 'market_session' OR (start_offset_min IS NOT NULL "
            "AND stop_offset_min IS NOT NULL)",
            name="ck_hosted_strategy_schedules_session_offsets",
        ),
        CheckConstraint(
            "schedule_kind = 'market_session' OR "
            "(start_offset_min IS NULL AND stop_offset_min IS NULL)",
            name="ck_hosted_strategy_schedules_non_session_offsets",
        ),
        CheckConstraint(
            "day_of_month IS NULL OR (day_of_month >= 1 AND day_of_month <= 31)",
            name="ck_hosted_strategy_schedules_day_of_month",
        ),
        CheckConstraint(
            "schedule_kind <> 'monthly' OR day_of_month IS NOT NULL",
            name="ck_hosted_strategy_schedules_monthly_day",
        ),
        CheckConstraint(
            "schedule_kind <> 'calendar' OR calendar_dates IS NOT NULL",
            name="ck_hosted_strategy_schedules_calendar_dates",
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
    #: Supervisor-reported process-cleanup evidence, bound to this attempt.
    #: ``None`` = unknown/unreported, ``confirmed`` = the supervisor proved the
    #: child process group is gone, ``unresolved`` = it could not. Only the
    #: supervisor lifecycle API (never the child) writes these.
    process_cleanup_state = Column(Text, nullable=True)
    process_cleanup_at = Column(DateTime(timezone=True), nullable=True)
    process_cleanup_actor = Column(Text, nullable=True)
    #: Supervisor-reported end-of-child report, bound to this attempt and written
    #: only by the supervisor lifecycle API: ``exited`` (clean script exit),
    #: ``stop_requested`` (operator asked the attempt to stop) or ``timeout``
    #: (observation bound reached). ``NULL`` means the attempt did not report a
    #: normal end (a fence, a lease expiry, a crash recovery) and is therefore
    #: never eligible for automatic evaluation continuation.
    completion_state = Column(Text, nullable=True)
    completion_at = Column(DateTime(timezone=True), nullable=True)
    #: Operator stop request, bound to the immutable attempt. ``desired_state``
    #: becomes ``stopped`` while the supervisor keeps its authority to perform a
    #: bounded local cleanup and the authorized terminal transition.
    stop_requested_at = Column(DateTime(timezone=True), nullable=True)
    stop_requested_by = Column(Text, nullable=True)
    #: Bounded-log contract: output was actually discarded at the cap, and how
    #: logs were collected (v1: ``post_termination`` only).
    logs_discarded = Column(Boolean, nullable=False, default=False)
    logs_source = Column(Text, nullable=True)
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
            "execution_mode IN ('paper', 'dry_run', 'live')",
            name="ck_strategy_jobs_execution_mode",
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
        CheckConstraint(
            "process_cleanup_state IS NULL OR process_cleanup_state IN ('confirmed', 'unresolved')",
            name="ck_strategy_jobs_process_cleanup_state",
        ),
        CheckConstraint(
            "completion_state IS NULL OR completion_state IN ('exited', 'stop_requested', 'timeout')",
            name="ck_strategy_jobs_completion_state",
        ),
        Index("idx_strategy_jobs_lease", "status", "lease_until"),
        Index("idx_strategy_jobs_owner_strategy", "owner_id", "strategy_id"),
    )


class StrategyJobReconciliation(Base):
    """Append-only audit of operator reconciliation attempts and evidence.

    History is never overwritten: every inspection-driven action writes a row
    with the evidence snapshot, the outcome and the server-derived actor. A job's
    block is cleared only when server-side evidence supports it.
    """

    __tablename__ = "strategy_job_reconciliations"

    id = Column(Text, primary_key=True)
    job_id = Column(
        Text, ForeignKey("strategy_jobs.id", ondelete="CASCADE"), nullable=False
    )
    strategy_id = Column(Text, nullable=False)
    owner_id = Column(Text, nullable=False)
    attempt = Column(Integer, nullable=False)
    run_id = Column(Text, nullable=True)
    outcome = Column(Text, nullable=False)
    reason_code = Column(Text, nullable=False)
    evidence_json = Column(JSON, nullable=False, default=dict)
    actor_id = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('reconciled', 'blocked', 'continuation', 'option_run_repair', "
            "'owner_action')",
            name="ck_strategy_job_reconciliations_outcome",
        ),
        CheckConstraint("attempt > 0", name="ck_strategy_job_reconciliations_attempt"),
        Index("idx_strategy_job_reconciliations_job", "job_id", "created_at"),
    )


class StrategyJobLog(Base):
    """Bounded, redacted child-log chunks shipped by the supervised runner.

    The API never reads the supervisor container's filesystem; the supervisor
    pushes bounded chunks through the lifecycle API, which redacts known
    credentials before they are persisted or presented to a browser.
    """

    __tablename__ = "strategy_job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Text, ForeignKey("strategy_jobs.id", ondelete="CASCADE"), nullable=False)
    attempt = Column(Integer, nullable=False)
    seq = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    #: Exact UTF-8 byte length, so the cap is accounted in bytes consistently.
    byte_len = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("job_id", "attempt", "seq", name="uq_strategy_job_logs_seq"),
        Index("idx_strategy_job_logs_job", "job_id", "attempt", "seq"),
    )


# ---------------------------------------------------------------------------
# Governed execution authorization (Phase 2)
# ---------------------------------------------------------------------------


class HostedExecutionGrant(Base):
    """An owner-issued, version-bound standing authorisation (Phase 2).

    A grant is *identity*, not a policy store: it binds the hosted strategy, the
    immutable version and its source hash, the canonical account, the execution
    environment, and a canonical hash of the validated admission policy plus the
    strategy's mandatory run-protection policy. It never carries a user's
    capital or loss tolerance of its own — those are read from the recorded
    policy and hashed, so a policy change invalidates the grant instead of
    silently widening it.

    The row is **immutable in its identity**: the database trigger added by the
    migration refuses an ``UPDATE`` that changes any identity column, refuses a
    DELETE, and refuses a row that stopped being ``active`` from becoming
    ``active`` again (a revoked or superseded grant can never be resurrected).
    Only the revocation/supersession columns and ``status`` may move.

    ``uq_hosted_execution_grant_active`` is the partial unique index that makes
    "one active grant per (strategy, account, environment)" a database fact
    rather than a read-then-write race; ``uq_hosted_execution_grant_request``
    makes an identical owner request idempotent instead of a second grant.
    """

    __tablename__ = "hosted_execution_grants"

    grant_id = Column(Text, primary_key=True)
    #: The app owner (``app:<username>``). Server-derived; never actor-supplied.
    owner_id = Column(Text, nullable=False)
    #: The hosted strategy this grant authorises.
    strategy_id = Column(Text, nullable=False)
    #: The canonical product identity the hosted strategy is an adapter over.
    #: Recorded explicitly so the binding is legible without a join.
    canonical_strategy_id = Column(Text, nullable=False)
    version_id = Column(Text, nullable=False)
    version_number = Column(Integer, nullable=False)
    source_sha256 = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    policy_hash = Column(Text, nullable=False)
    policy_snapshot = Column(JSON, nullable=False, default=dict)
    issued_by = Column(Text, nullable=False)
    issued_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(Text, nullable=False, default="active")
    revoked_by = Column(Text, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revocation_reason = Column(Text, nullable=True)
    superseded_by = Column(Text, nullable=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    supersession_reason = Column(Text, nullable=True)
    #: The caller's idempotency key, plus the canonical hash of what the request
    #: actually asked for. A repeat with the same key AND the same content is
    #: idempotent; the same key with different content is a conflict.
    request_key = Column(Text, nullable=False)
    content_sha256 = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "strategy_id", "request_key",
            name="uq_hosted_execution_grant_request",
        ),
        Index(
            "uq_hosted_execution_grant_active",
            "strategy_id",
            "account_id",
            "execution_environment",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        ForeignKeyConstraint(
            ["strategy_id", "owner_id"],
            ["hosted_strategies.id", "hosted_strategies.owner_id"],
            ondelete="CASCADE",
            name="fk_hosted_execution_grant_strategy_owner",
        ),
        ForeignKeyConstraint(
            ["version_id", "strategy_id"],
            ["hosted_strategy_versions.id", "hosted_strategy_versions.strategy_id"],
            ondelete="RESTRICT",
            name="fk_hosted_execution_grant_version_strategy",
        ),
        # Account agreement is database-enforced against the CANONICAL strategy,
        # so a grant can never name an account the strategy is not pinned to.
        ForeignKeyConstraint(
            ["canonical_strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            ondelete="RESTRICT",
            name="fk_hosted_execution_grant_canonical",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'superseded')",
            name="ck_hosted_execution_grant_status",
        ),
        CheckConstraint(
            "execution_environment IN ('paper', 'dry_run', 'live')",
            name="ck_hosted_execution_grant_environment",
        ),
        CheckConstraint("version_number > 0", name="ck_hosted_execution_grant_version"),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_at IS NULL AND revoked_by IS NULL)",
            name="ck_hosted_execution_grant_revocation",
        ),
        CheckConstraint(
            "(status = 'superseded' AND superseded_by IS NOT NULL "
            "AND superseded_at IS NOT NULL) "
            "OR (status <> 'superseded' AND superseded_by IS NULL "
            "AND superseded_at IS NULL)",
            name="ck_hosted_execution_grant_supersession",
        ),
        Index("idx_hosted_execution_grant_strategy", "strategy_id", "created_at"),
    )


class HostedExecutionRequest(Base):
    """One durable, idempotent request to execute one frozen plan (Phase 2).

    A proposal stays a proposal until execution is *requested*. This row is that
    request: it records the originating run/job/version, the authorization mode
    and (for autonomous mode) the grant, the decision evidence, the linked
    reservation/approval/execution trail, and the durable dispatch claim.

    ``uq_hosted_execution_requests_key`` makes a repeated call with the same
    idempotency key return the same request; a repeated key with different
    content is a conflict. The dispatcher's claim is an ``UPDATE`` guarded by the
    hosted-strategy row lock, so revocation and claim acquisition linearise on
    one row.
    """

    __tablename__ = "hosted_execution_requests"

    request_id = Column(Text, primary_key=True)
    owner_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    canonical_strategy_id = Column(Text, nullable=False)
    account_id = Column(Text, nullable=False)
    execution_environment = Column(Text, nullable=False)
    strategy_run_id = Column(Text, nullable=False)
    job_id = Column(Text, nullable=True)
    #: The child credential the request was created under. A rotated token is a
    #: different attempt, so the dispatch fence compares it.
    token_id = Column(Text, nullable=True)
    attempt = Column(Integer, nullable=True)
    lease_epoch = Column(BigInteger, nullable=True)
    version_id = Column(Text, nullable=False)
    version_number = Column(Integer, nullable=True)
    source_sha256 = Column(Text, nullable=False)
    policy_hash = Column(Text, nullable=False)
    evaluation_id = Column(Text, nullable=True)
    plan_id = Column(Text, nullable=False)
    plan_hash = Column(Text, nullable=False)
    authorization_mode = Column(Text, nullable=False)
    grant_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="requested")
    refusal_code = Column(Text, nullable=True)
    refusal_detail = Column(JSON, nullable=False, default=dict)
    decision_kind = Column(Text, nullable=True)
    decision_actor = Column(Text, nullable=True)
    decision_at = Column(DateTime(timezone=True), nullable=True)
    decision_evidence = Column(JSON, nullable=False, default=dict)
    approval_id = Column(Text, nullable=True)
    reservation_id = Column(Text, nullable=True)
    execution_detail = Column(JSON, nullable=False, default=dict)
    dispatch_claim_id = Column(Text, nullable=True)
    dispatch_claimed_at = Column(DateTime(timezone=True), nullable=True)
    dispatch_started_at = Column(DateTime(timezone=True), nullable=True)
    dispatch_finished_at = Column(DateTime(timezone=True), nullable=True)
    idempotency_key = Column(Text, nullable=False)
    request_hash = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "plan_id", "idempotency_key",
            name="uq_hosted_execution_requests_key",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "owner_id"],
            ["hosted_strategies.id", "hosted_strategies.owner_id"],
            ondelete="CASCADE",
            name="fk_hosted_execution_request_strategy_owner",
        ),
        ForeignKeyConstraint(
            ["canonical_strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            ondelete="RESTRICT",
            name="fk_hosted_execution_request_canonical",
        ),
        ForeignKeyConstraint(
            ["plan_id"],
            ["strategy_plans.plan_id"],
            ondelete="RESTRICT",
            name="fk_hosted_execution_request_plan",
        ),
        ForeignKeyConstraint(
            ["grant_id"],
            ["hosted_execution_grants.grant_id"],
            ondelete="RESTRICT",
            name="fk_hosted_execution_request_grant",
        ),
        CheckConstraint(
            "status IN ('requested', 'awaiting_approval', 'queued', 'dispatching', "
            "'executed', 'refused', 'rejected', 'dispatch_unresolved')",
            name="ck_hosted_execution_request_status",
        ),
        CheckConstraint(
            "authorization_mode IN ('approval_based', 'autonomous')",
            name="ck_hosted_execution_request_mode",
        ),
        CheckConstraint(
            "execution_environment IN ('paper', 'dry_run', 'live')",
            name="ck_hosted_execution_request_environment",
        ),
        Index("idx_hosted_execution_request_strategy", "strategy_id", "created_at"),
        Index("idx_hosted_execution_request_state", "status", "created_at"),
        Index("idx_hosted_execution_request_run", "strategy_run_id", "created_at"),
    )


class HostedExecutionAudit(Base):
    """Append-only audit of authorization and dispatch decisions (Phase 2).

    Every mode change, grant issue/revoke/supersede, request decision, dispatch
    claim and terminal outcome lands here with a server-derived actor. The
    migration installs an insert-only trigger, so an audit row can never be
    rewritten or deleted — it is the durable "who decided what, against which
    evidence" record for the governed execution path.
    """

    __tablename__ = "hosted_execution_audit"

    audit_id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Text, nullable=False)
    strategy_id = Column(Text, nullable=False)
    subject_kind = Column(Text, nullable=False)
    subject_id = Column(Text, nullable=False)
    event = Column(Text, nullable=False)
    actor_id = Column(Text, nullable=False)
    #: ``owner`` | ``system`` | ``automatic_grant`` — never a caller claim.
    actor_kind = Column(Text, nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "subject_kind IN ('grant', 'mode', 'request', 'dispatch')",
            name="ck_hosted_execution_audit_subject",
        ),
        CheckConstraint(
            "actor_kind IN ('owner', 'system', 'automatic_grant')",
            name="ck_hosted_execution_audit_actor_kind",
        ),
        Index("idx_hosted_execution_audit_strategy", "strategy_id", "created_at"),
        Index("idx_hosted_execution_audit_subject", "subject_kind", "subject_id"),
    )


__all__ = [
    "HostedStrategy",
    "HostedStrategySchedule",
    "HostedStrategyVersion",
    "HostedExecutionAudit",
    "HostedExecutionGrant",
    "HostedExecutionRequest",
    "StrategyJob",
    "StrategyJobLog",
    "StrategyJobReconciliation",
]
