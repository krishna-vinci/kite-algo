"""bounded log contract: UTF-8 byte accounting + discarded/source flags.

Revision ID: 20260915_000024
Revises: 20260915_000023
Create Date: 2026-09-15 00:00:24

Purely additive:

- ``strategy_job_logs.byte_len`` stores the exact UTF-8 byte length of each
  stored chunk, so the cap is accounted in bytes consistently (not characters).
- ``strategy_jobs.logs_discarded`` records that output was actually dropped at
  the cap (browser truncation must reflect real loss).
- ``strategy_jobs.logs_source`` records how logs were collected; v1 only ships
  after termination, so this is ``post_termination`` (live collection is not
  implemented).
"""

from alembic import op

revision = "20260915_000024"
down_revision = "20260915_000023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_job_logs ADD COLUMN IF NOT EXISTS byte_len INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs ADD COLUMN IF NOT EXISTS logs_discarded BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE public.strategy_jobs ADD COLUMN IF NOT EXISTS logs_source TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS logs_source")
    op.execute("ALTER TABLE public.strategy_jobs DROP COLUMN IF EXISTS logs_discarded")
    op.execute("ALTER TABLE public.strategy_job_logs DROP COLUMN IF EXISTS byte_len")
