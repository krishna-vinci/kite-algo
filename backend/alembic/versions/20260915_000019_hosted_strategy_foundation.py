"""hosted strategy foundation: store, versions, schedules, jobs.

Revision ID: 20260915_000019
Revises: 20260912_000018
Create Date: 2026-09-15 00:00:19

Purely additive: four new tables, no change to any existing table, so it is safe
to apply before the code that reads it and needs no backfill.

This is the SCHEMA + AUTHORIZATION foundation only. It stores hosted strategy
source/versions, bounded schedule configuration, and a job ledger with
lease-epoch/attempt fencing. It deliberately creates **no** execution path: no
runner, child process, worker-run or worker-token creation, notifications,
orders, or frontend.

Structural guarantees (enforced by columns/constraints, not convention):

- **Identity relations are composite FKs.** A job or schedule can only reference
  a version that belongs to its strategy and a strategy owned by the recorded
  owner: ``(strategy_id, owner_id) -> hosted_strategies(id, owner_id)`` and
  ``(version_id, strategy_id) -> hosted_strategy_versions(id, strategy_id)``.
- **Snapshots, not defaults.** Jobs and schedules carry immutable
  ``account_scope`` / ``params_snapshot`` / ``capabilities_snapshot`` /
  ``policy_snapshot`` and effective ``max_duration_s`` / ``progress_deadline_s``,
  so a queued job is never reconstructed from mutable strategy defaults.
- **Fencing is a column comparison.** ``lease_owner`` / ``lease_epoch`` /
  ``lease_until`` plus ``attempt`` fence transitions; ``recovery_required``
  blocks replacement until reconciliation. ``run_id`` is TEXT to match
  ``algo_worker_runs.strategy_run_id`` (TEXT), not UUID.

Execution modes are restricted to paper/dry_run by CHECK; ``job_kind``
(continuous/finite) is separate from ``execution_mode``. Scheduling is stored but
not exposed by this slice; the trigger is an explicit clock time plus weekday
(``session_close`` deferred — its completion window is ambiguous).
"""

from alembic import op

revision = "20260915_000019"
down_revision = "20260912_000018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.hosted_strategies (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            name TEXT NOT NULL,
            template_id TEXT NOT NULL,
            description TEXT,
            default_execution_mode TEXT NOT NULL DEFAULT 'paper',
            default_job_kind TEXT NOT NULL DEFAULT 'finite',
            default_account_scope TEXT NOT NULL,
            max_duration_s INTEGER NOT NULL,
            progress_deadline_s INTEGER NOT NULL,
            stale_exit_policy TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_hosted_strategies_owner_name UNIQUE (owner_id, name),
            CONSTRAINT uq_hosted_strategies_template UNIQUE (template_id),
            CONSTRAINT uq_hosted_strategies_id_owner UNIQUE (id, owner_id),
            CONSTRAINT ck_hosted_strategies_template_id CHECK (template_id = 'hosted:' || id),
            CONSTRAINT ck_hosted_strategies_execution_mode
                CHECK (default_execution_mode IN ('paper', 'dry_run')),
            CONSTRAINT ck_hosted_strategies_job_kind
                CHECK (default_job_kind IN ('continuous', 'finite')),
            CONSTRAINT ck_hosted_strategies_max_duration CHECK (max_duration_s > 0),
            CONSTRAINT ck_hosted_strategies_progress_deadline CHECK (progress_deadline_s > 0),
            CONSTRAINT ck_hosted_strategies_stale_policy
                CHECK (stale_exit_policy IN ('none', 'exit_on_worker_stale')),
            CONSTRAINT ck_hosted_strategies_status
                CHECK (status IN ('active', 'disabled'))
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hosted_strategies_owner
            ON public.hosted_strategies (owner_id);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.hosted_strategy_versions (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES public.hosted_strategies(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            parameters_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
            capabilities_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_hosted_strategy_versions_number UNIQUE (strategy_id, version),
            CONSTRAINT uq_hosted_strategy_versions_id_strategy UNIQUE (id, strategy_id),
            CONSTRAINT ck_hosted_strategy_versions_number CHECK (version > 0)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hosted_strategy_versions_strategy
            ON public.hosted_strategy_versions (strategy_id, version DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.hosted_strategy_schedules (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            params_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            execution_mode TEXT NOT NULL,
            job_kind TEXT NOT NULL,
            policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            capabilities_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            max_duration_s INTEGER NOT NULL,
            progress_deadline_s INTEGER NOT NULL,
            schedule_kind TEXT NOT NULL,
            at_time TEXT NOT NULL,
            weekday INTEGER,
            timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
            window_end TEXT,
            squareoff_at TEXT,
            enabled BOOLEAN NOT NULL DEFAULT true,
            manual_paused_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_hosted_strategy_schedules_strategy UNIQUE (strategy_id),
            CONSTRAINT fk_hosted_strategy_schedules_strategy_owner
                FOREIGN KEY (strategy_id, owner_id)
                REFERENCES public.hosted_strategies (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_hosted_strategy_schedules_version_strategy
                FOREIGN KEY (version_id, strategy_id)
                REFERENCES public.hosted_strategy_versions (id, strategy_id) ON DELETE RESTRICT,
            CONSTRAINT ck_hosted_strategy_schedules_execution_mode
                CHECK (execution_mode IN ('paper', 'dry_run')),
            CONSTRAINT ck_hosted_strategy_schedules_job_kind
                CHECK (job_kind IN ('continuous', 'finite')),
            CONSTRAINT ck_hosted_strategy_schedules_kind
                CHECK (schedule_kind IN ('daily', 'weekly')),
            CONSTRAINT ck_hosted_strategy_schedules_weekday
                CHECK (weekday IS NULL OR (weekday >= 0 AND weekday <= 6)),
            CONSTRAINT ck_hosted_strategy_schedules_weekly_weekday
                CHECK (schedule_kind <> 'weekly' OR weekday IS NOT NULL),
            CONSTRAINT ck_hosted_strategy_schedules_max_duration CHECK (max_duration_s > 0),
            CONSTRAINT ck_hosted_strategy_schedules_progress_deadline
                CHECK (progress_deadline_s > 0)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.strategy_jobs (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            job_kind TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            desired_state TEXT NOT NULL DEFAULT 'started',
            occurrence_key TEXT,
            run_id TEXT,
            token_id TEXT,
            lease_owner TEXT,
            lease_epoch BIGINT NOT NULL DEFAULT 0,
            lease_until TIMESTAMPTZ,
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            params_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            capabilities_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            max_duration_s INTEGER NOT NULL,
            progress_deadline_s INTEGER NOT NULL,
            identity_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_progress_at TIMESTAMPTZ,
            exit_code INTEGER,
            log_ref TEXT,
            recovery_required_at TIMESTAMPTZ,
            reconciled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_strategy_jobs_occurrence UNIQUE (occurrence_key),
            CONSTRAINT fk_strategy_jobs_strategy_owner
                FOREIGN KEY (strategy_id, owner_id)
                REFERENCES public.hosted_strategies (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_strategy_jobs_version_strategy
                FOREIGN KEY (version_id, strategy_id)
                REFERENCES public.hosted_strategy_versions (id, strategy_id) ON DELETE RESTRICT,
            CONSTRAINT ck_strategy_jobs_job_kind CHECK (job_kind IN ('continuous', 'finite')),
            CONSTRAINT ck_strategy_jobs_execution_mode CHECK (execution_mode IN ('paper', 'dry_run')),
            CONSTRAINT ck_strategy_jobs_desired_state
                CHECK (desired_state IN ('started', 'paused', 'stopped')),
            CONSTRAINT ck_strategy_jobs_attempt CHECK (attempt > 0),
            CONSTRAINT ck_strategy_jobs_lease_epoch CHECK (lease_epoch >= 0),
            CONSTRAINT ck_strategy_jobs_max_duration CHECK (max_duration_s > 0),
            CONSTRAINT ck_strategy_jobs_progress_deadline CHECK (progress_deadline_s > 0),
            CONSTRAINT ck_strategy_jobs_status CHECK (
                status IN (
                    'queued', 'starting', 'running', 'fencing',
                    'recovery_required', 'stopped', 'failed', 'hung'
                )
            )
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_jobs_lease
            ON public.strategy_jobs (status, lease_until);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_jobs_owner_strategy
            ON public.strategy_jobs (owner_id, strategy_id);
        """
    )


def downgrade() -> None:
    # Additive slice: dropping the ledger and store costs configuration that can
    # be re-created, and touches no existing table. Child tables first.
    op.execute("DROP TABLE IF EXISTS public.strategy_jobs")
    op.execute("DROP TABLE IF EXISTS public.hosted_strategy_schedules")
    op.execute("DROP TABLE IF EXISTS public.hosted_strategy_versions")
    op.execute("DROP TABLE IF EXISTS public.hosted_strategies")
