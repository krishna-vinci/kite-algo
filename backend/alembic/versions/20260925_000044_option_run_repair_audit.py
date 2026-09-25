"""option-run repair: a distinct audit outcome for an owner-driven repair.

Revision ID: 20260925_000044
Revises: 20260924_000043
Create Date: 2026-09-25

Purely additive. ``strategy_job_reconciliations.outcome`` gains the value
``option_run_repair``, so a governed repair of ONE option run (B2.1b) lands in the
SAME append-only audit table as operator reconciliation and evaluation
continuation - distinct from ``reconciled`` (a human cleared the job's block) and
from ``continuation`` (an automatic handover of a held book).

The distinction is not cosmetic: the repair path never touches the job's block
state, so labelling its rows ``reconciled`` would make the job's reconciliation
history (``GET /{strategy_id}/jobs/{job_id}/reconciliation``, which reads
``list_reconciliations``) claim a reconciliation that never happened.

The widening CHECK accepts every existing value, so nothing is backfilled and no
other object is altered.
"""

from alembic import op

revision = "20260925_000044"
down_revision = "20260924_000043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "DROP CONSTRAINT IF EXISTS ck_strategy_job_reconciliations_outcome"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "ADD CONSTRAINT ck_strategy_job_reconciliations_outcome "
        "CHECK (outcome IN ('reconciled', 'blocked', 'continuation', 'option_run_repair'))"
    )


def downgrade() -> None:
    # Narrowing the CHECK can only succeed when no repair row exists, and the
    # constraint itself is the guard: a row the repair path wrote is never
    # silently dropped and never becomes an unlabelled "reconciled". The operator
    # archives deliberately and re-runs the downgrade, which is the same
    # fix-forward posture 000043 takes for ``continuation``.
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "DROP CONSTRAINT IF EXISTS ck_strategy_job_reconciliations_outcome"
    )
    op.execute(
        "ALTER TABLE public.strategy_job_reconciliations "
        "ADD CONSTRAINT ck_strategy_job_reconciliations_outcome "
        "CHECK (outcome IN ('reconciled', 'blocked', 'continuation'))"
    )
