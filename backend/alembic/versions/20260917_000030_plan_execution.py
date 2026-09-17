"""Plan execution event trail and the bounded bundle vocabulary (Project 6).

Revision ID: 20260917_000030
Revises: 20260917_000029
Create Date: 2026-09-17

Why each change exists (R3 §9/§10, plan D-3/D-6):

- ``strategy_plan_execution_events`` is the append-only trail of one plan's
  execution (D-3). It is the ONLY execution state the schema carries: the
  current state of a step is derived from its rows (``submitted`` → ``filled``
  | ``rejected`` | ``failed``; ``no_op`` is terminal in itself), so execution
  history can never be rewritten into something that did not happen. Every
  refusal is an event with its named reason, and every paper order is linked by
  ``paper_order_id`` so the fill-to-attribution chain (G1's paper fold) reaches
  the plan, the step and the reservation from the fill fact alone.

- The ``ck_proposals_target_kind`` and ``ck_plans_plan_kind`` vocabularies gain
  ``intent_bundle`` (D-6): a bounded bundle resolves to explicit per-leg
  single-instrument actions, so the platform must be able to store one. The
  change is purely additive — the allowed set only grows, and every previously
  valid row stays valid. Nothing else is altered.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000030"
down_revision = "20260917_000029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_plan_execution_events",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("paper_order_id", sa.Text(), nullable=True),
        sa.Column("filled_quantity", sa.Integer(), nullable=True),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event IN ('submitted','filled','rejected','failed','no_op')",
            name="ck_spee_event",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["public.strategy_plans.plan_id"],
            name="strategy_plan_execution_events_plan_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_plan_exec_events",
        "strategy_plan_execution_events",
        ["plan_id", "step_no", "created_at"],
    )

    # Append-only enforcement (D-3): a submission, an outcome or a refusal is a
    # fact; rewriting it would make execution history an opinion.
    op.execute(
        """
        CREATE FUNCTION forbid_strategy_plan_execution_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_plan_execution_events are append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_plan_execution_events_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_plan_execution_events
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_plan_execution_event_mutation();
        """
    )

    # ``intent_bundle`` becomes a storable plan kind (D-6). Additive only: the
    # allowed vocabulary grows; no existing row or behaviour changes.
    op.execute(
        "ALTER TABLE public.strategy_proposals DROP CONSTRAINT ck_proposals_target_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals ADD CONSTRAINT ck_proposals_target_kind "
        "CHECK (target_kind IN ('single_instrument','target_weights','intent_bundle'))"
    )
    op.execute("ALTER TABLE public.strategy_plans DROP CONSTRAINT ck_plans_plan_kind")
    op.execute(
        "ALTER TABLE public.strategy_plans ADD CONSTRAINT ck_plans_plan_kind "
        "CHECK (plan_kind IN ('single_instrument','target_weights','intent_bundle'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_plans DROP CONSTRAINT ck_plans_plan_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_plans ADD CONSTRAINT ck_plans_plan_kind "
        "CHECK (plan_kind IN ('single_instrument','target_weights'))"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals DROP CONSTRAINT ck_proposals_target_kind"
    )
    op.execute(
        "ALTER TABLE public.strategy_proposals ADD CONSTRAINT ck_proposals_target_kind "
        "CHECK (target_kind IN ('single_instrument','target_weights'))"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_plan_execution_events_immutable "
        "ON public.strategy_plan_execution_events"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_plan_execution_event_mutation()")
    op.drop_index("idx_plan_exec_events", table_name="strategy_plan_execution_events")
    op.drop_table("strategy_plan_execution_events")
