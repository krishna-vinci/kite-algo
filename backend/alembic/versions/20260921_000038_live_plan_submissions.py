"""Durable claim/outcome row for one live plan step.

Revision ID: 20260921_000038
Revises: 20260921_000037
Create Date: 2026-09-21

Why this exists:

- The internal live adapter must not keep its submission state in process
  memory: two adapter instances (or a restart) could then dispatch the same plan
  step twice, and an unconfirmed outcome would die with the process.

- ``UNIQUE (plan_id, step_no)`` makes the claim atomic. The loser of the insert
  reads the winner's row instead of dispatching; ``state`` records
  ``pending``/``uncertain``/``rejected``, so "no order id came back" is an
  UNCERTAIN outcome that is never repeated and never treated as a rejection.

- Purely additive: one new table plus an index. The downgrade drops exactly what
  the upgrade created.
"""

from alembic import op

revision = "20260921_000038"
down_revision = "20260921_000037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.live_plan_submissions (
            submission_id TEXT PRIMARY KEY,
            plan_id UUID NOT NULL,
            step_no INTEGER NOT NULL,
            step_ref TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            execution_environment TEXT NOT NULL,
            state TEXT NOT NULL,
            broker_order_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            delta_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            detail JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_live_plan_step UNIQUE (plan_id, step_no),
            CONSTRAINT ck_live_plan_submission_state
                CHECK (state IN ('pending', 'uncertain', 'rejected', 'no_op')),
            CONSTRAINT fk_live_plan_submission_plan FOREIGN KEY (plan_id)
                REFERENCES public.strategy_plans (plan_id) ON DELETE RESTRICT,
            CONSTRAINT fk_live_plan_submission_strategy
                FOREIGN KEY (strategy_id, account_id)
                REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_plan_submission_state "
        "ON public.live_plan_submissions (state)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.live_plan_submissions")
