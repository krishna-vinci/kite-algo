"""evaluation continuation: auditable automatic handover of a held book.

Revision ID: 20260924_000043
Revises: 20260923_000042
Create Date: 2026-09-24

Purely additive. Two things become representable:

1. ``strategy_jobs.completion_state``/``completion_at`` — the supervisor-reported
   end-of-attempt report for THIS attempt, written only by the supervisor
   lifecycle API. It is the durable marker that a FINITE attempt ended in a
   clean script exit (``exited``) rather than an operator stop, an observation
   timeout, a fence or a crash recovery, so the shared Run now/scheduled-job
   path can finish a continuation proof after a host restart without ever
   guessing.
2. ``strategy_job_reconciliations.outcome`` gains the value ``continuation``, so
   a server-derived automatic handover of a FINITE evaluation that finished
   cleanly with an intentionally held book is recorded in the SAME append-only
   audit table as operator reconciliation - distinct from ``reconciled`` (a human
   cleared the block) and from ``blocked`` (evidence refused). Reusing the audit
   table is the smallest change that keeps one history per attempt; no new ledger
   is created and no existing row changes meaning.

Nothing else is altered destructively and no backfill is required: the widening
CHECK accepts every existing value.
"""

from alembic import op

revision = "20260924_000043"
down_revision = "20260923_000042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS completion_state TEXT"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD COLUMN IF NOT EXISTS completion_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "DROP CONSTRAINT IF EXISTS ck_strategy_jobs_completion_state"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "ADD CONSTRAINT ck_strategy_jobs_completion_state "
        "CHECK (completion_state IS NULL OR completion_state IN "
        "('exited', 'stop_requested', 'timeout'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "DROP CONSTRAINT IF EXISTS ck_strategy_job_reconciliations_outcome"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "ADD CONSTRAINT ck_strategy_job_reconciliations_outcome "
        "CHECK (outcome IN ('reconciled', 'blocked', 'continuation'))"
    )


def downgrade() -> None:
    # Narrowing the CHECK can only succeed when no continuation row exists; the
    # constraint itself is the guard, so the loss is never silent. The operator
    # archives deliberately and re-runs the downgrade, which is the same
    # fix-forward posture the neighbouring revisions take.
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "DROP CONSTRAINT IF EXISTS ck_strategy_job_reconciliations_outcome"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "ADD CONSTRAINT ck_strategy_job_reconciliations_outcome "
        "CHECK (outcome IN ('reconciled', 'blocked'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs "
        "DROP CONSTRAINT IF EXISTS ck_strategy_jobs_completion_state"
    )
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS completion_at")
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS completion_state")
