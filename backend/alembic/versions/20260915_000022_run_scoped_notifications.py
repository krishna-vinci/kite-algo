"""run-scoped notifications: signal_events source/owner/run columns.

Revision ID: 20260915_000022
Revises: 20260915_000021
Create Date: 2026-09-15 00:00:22

Purely additive. Extends ``signal_events`` so a strategy-run notification can
reuse the existing durable event/outbox/delivery infrastructure without changing
the ``deliveries`` FK:

- ``source_kind`` (TEXT NOT NULL DEFAULT 'workflow') — existing rows stay
  ``workflow``; hosted run events use ``strategy_run``.
- ``owner_id`` (TEXT NULL) — the hosted strategy's **app owner**, persisted as an
  explicit binding. It is NOT an account scope and NOT a worker-token owner.
- ``run_id`` (TEXT NULL) — matches ``algo_worker_runs.strategy_run_id`` (TEXT).
- an index on ``run_id`` for run-scoped history.

No FK change; alert/screener rows are unaffected.
"""

from alembic import op

revision = "20260915_000022"
down_revision = "20260915_000021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.signal_events "
        "ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'workflow'"
    )
    op.execute(
        "ALTER TABLE public.signal_events ADD COLUMN IF NOT EXISTS owner_id TEXT"
    )
    op.execute(
        "ALTER TABLE public.signal_events ADD COLUMN IF NOT EXISTS run_id TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_events_run "
        "ON public.signal_events (run_id, fired_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_events_owner "
        "ON public.signal_events (owner_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_signal_events_owner")
    op.execute("DROP INDEX IF EXISTS public.idx_signal_events_run")
    op.execute("ALTER TABLE public.signal_events DROP COLUMN IF EXISTS run_id")
    op.execute("ALTER TABLE public.signal_events DROP COLUMN IF EXISTS owner_id")
    op.execute("ALTER TABLE public.signal_events DROP COLUMN IF EXISTS source_kind")
