"""Durable edge from a frozen option-structure plan to its option run.

Revision ID: 20260921_000037
Revises: 20260921_000036
Create Date: 2026-09-21

Why this exists:

- The options lane executes through the existing ``option_run_states`` engine
  (lifecycle helpers + ``DurableOptionRunStore``), whose primary key is the
  option-run id. Before this edge the paper plan executor had no durable way to
  say *which* run a frozen entry plan created, or *which* run an exit plan
  closes, so the two identities risked being conflated with the hosted worker
  run id.

- The relation keeps them separate: ``plan_id`` is unique (a retry resolves to
  the same binding, never a second run), an entry plan creates at most one run
  (the partial unique index), and any number of exit plans may reference the same
  run.

- Purely additive: one new table plus indexes. The downgrade drops exactly what
  the upgrade created, so a prior-head database is restored verbatim.
"""

from alembic import op

revision = "20260921_000037"
down_revision = "20260921_000036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.strategy_plan_option_runs (
            plan_id UUID PRIMARY KEY,
            option_run_id TEXT NOT NULL,
            worker_run_id TEXT,
            strategy_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            execution_environment TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_plan_option_run_phase CHECK (phase IN ('entry', 'exit')),
            CONSTRAINT fk_plan_option_run_plan FOREIGN KEY (plan_id)
                REFERENCES public.strategy_plans (plan_id) ON DELETE RESTRICT,
            CONSTRAINT fk_plan_option_run_run FOREIGN KEY (option_run_id)
                REFERENCES public.option_run_states (strategy_run_id) ON DELETE RESTRICT,
            CONSTRAINT fk_plan_option_run_strategy
                FOREIGN KEY (strategy_id, account_id)
                REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plan_option_run_run "
        "ON public.strategy_plan_option_runs (option_run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plan_option_run_worker "
        "ON public.strategy_plan_option_runs (worker_run_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_option_run_entry "
        "ON public.strategy_plan_option_runs (option_run_id) WHERE phase = 'entry'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.strategy_plan_option_runs")
