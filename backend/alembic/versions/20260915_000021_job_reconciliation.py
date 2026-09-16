"""operator reconciliation: process-cleanup evidence + audit table.

Revision ID: 20260915_000021
Revises: 20260915_000020
Create Date: 2026-09-15 00:00:21

Purely additive. Two changes:

- ``strategy_jobs`` gains supervisor-reported process-cleanup evidence
  (``process_cleanup_state``/``_at``/``_actor``). It is written only by the
  supervisor lifecycle API, bound to the attempt, so the child cannot forge it.
- a new append-only ``strategy_job_reconciliations`` audit table records every
  operator reconciliation attempt with its evidence snapshot, outcome and
  server-derived actor. History is never overwritten.

No existing table is altered destructively and no backfill is required.
"""

from alembic import op

revision = "20260915_000021"
down_revision = "20260915_000020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS process_cleanup_state TEXT"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS process_cleanup_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS process_cleanup_actor TEXT"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_strategy_jobs_process_cleanup_state'
            ) THEN
                ALTER TABLE public.strategy_jobs
                    ADD CONSTRAINT ck_strategy_jobs_process_cleanup_state
                    CHECK (
                        process_cleanup_state IS NULL
                        OR process_cleanup_state IN ('confirmed', 'unresolved')
                    );
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.strategy_job_reconciliations (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES public.strategy_jobs(id) ON DELETE CASCADE,
            strategy_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            run_id TEXT,
            outcome TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            actor_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_strategy_job_reconciliations_outcome
                CHECK (outcome IN ('reconciled', 'blocked')),
            CONSTRAINT ck_strategy_job_reconciliations_attempt CHECK (attempt > 0)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_job_reconciliations_job
            ON public.strategy_job_reconciliations (job_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.strategy_job_reconciliations")
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "DROP CONSTRAINT IF EXISTS ck_strategy_jobs_process_cleanup_state"
    )
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS process_cleanup_actor")
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS process_cleanup_at")
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS process_cleanup_state")
