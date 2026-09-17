"""Proposal envelopes, frozen plans and the proposal journal.

Revision ID: 20260917_000027
Revises: 20260917_000026
Create Date: 2026-09-17

Purely additive: three new tables plus three insert-only triggers. Nothing
existing is altered.

Why each table exists (R3 §6, §7):

- ``strategy_proposals`` is the **durable, queryable-forever** envelope of one
  market decision. ``UNIQUE (strategy_id, evaluation_id)`` is the whole
  cardinality contract: one evaluation identity creates at most one envelope, an
  exact retry is idempotent, and a different payload under the same identity is a
  conflict — which is why a continuous job may hold many evaluations without the
  platform ever inventing an id. The composite FK to
  ``strategies (id, account_scope)`` mirrors ``strategy_run_bindings`` so
  strategy/account drift is database-refused rather than application-checked. The
  payload is stored verbatim beside its ``payload_sha256``, which is what makes
  "same evaluation, different decision" decidable after the fact.

- ``strategy_plans`` is the **immutable resolved artifact**: resolution happens
  once against a pinned catalog generation (R3 §7), so the row carries both the
  logical representation and the fully resolved execution representation, joined
  by ``plan_hash`` over the two plus the pin. ``pinned_catalog_generation`` is a
  real FK to the generation: a plan can never point at a generation that does not
  exist, and a plan is never rewritten when a newer generation arrives —
  invalidation is derived at read time, never stored. The ``target_weights``
  scope columns are NOT NULL for that kind by CHECK, because a full-snapshot plan
  without its revision and member hash could not honour "omission inside the
  scope means target zero".

- ``strategy_proposal_journal`` is the append-only trail of what happened to an
  evaluation: received, idempotent retry, conflict, validation refusal, plan
  created. It is the evidence R3 §19 requires, and it deliberately duplicates no
  state — the envelope and plan are the truth, the journal is the sequence.

Every one of the three is insert-only by trigger. There is no update and no
delete path: a correction is a NEW journal event, and a refused evaluation is
terminal by design.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000027"
down_revision = "20260917_000026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_proposals",
        sa.Column(
            "proposal_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("evaluation_id", sa.Text(), nullable=False),
        sa.Column("evaluation_kind", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("strategy_run_id", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "evaluation_kind IN ('scheduled_occurrence','run_now')",
            name="ck_proposals_evaluation_kind",
        ),
        sa.CheckConstraint(
            "target_kind IN ('single_instrument','target_weights')",
            name="ck_proposals_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('received','validated','refused')",
            name="ck_proposals_status",
        ),
        sa.CheckConstraint(
            "evaluation_kind <> 'scheduled_occurrence' OR job_id IS NOT NULL",
            name="ck_proposals_scheduled_requires_job",
        ),
        sa.UniqueConstraint("strategy_id", "evaluation_id", name="uq_proposals_strategy_evaluation"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["public.strategies.id", "public.strategies.account_scope"],
            name="strategy_proposals_strategy_id_account_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_proposals_strategy", "strategy_proposals", ["strategy_id", "created_at"]
    )

    op.create_table(
        "strategy_plans",
        sa.Column(
            "plan_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("plan_kind", sa.Text(), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("logical_plan", postgresql.JSONB(), nullable=False),
        sa.Column("resolved_plan", postgresql.JSONB(), nullable=False),
        sa.Column("pinned_universe_revision_id", sa.Text(), nullable=True),
        sa.Column("pinned_member_hash", sa.Text(), nullable=True),
        sa.Column("pinned_catalog_generation", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "plan_kind IN ('single_instrument','target_weights')",
            name="ck_plans_plan_kind",
        ),
        sa.CheckConstraint(
            "plan_kind <> 'target_weights' OR "
            "(pinned_universe_revision_id IS NOT NULL AND pinned_member_hash IS NOT NULL)",
            name="ck_plans_target_weights_scope",
        ),
        sa.UniqueConstraint("proposal_id", name="uq_plans_proposal"),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["public.strategy_proposals.proposal_id"],
            name="strategy_plans_proposal_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pinned_catalog_generation"],
            ["public.instrument_catalog_generations.id"],
            name="strategy_plans_pinned_catalog_generation_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["public.strategies.id", "public.strategies.account_scope"],
            name="strategy_plans_strategy_id_account_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("idx_plans_strategy", "strategy_plans", ["strategy_id", "created_at"])

    op.create_table(
        "strategy_proposal_journal",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("evaluation_id", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event IN ('received','idempotent_retry','conflict','validation_refused','plan_created')",
            name="ck_proposal_journal_event",
        ),
    )
    op.create_index(
        "idx_proposal_journal_strategy",
        "strategy_proposal_journal",
        ["strategy_id", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_proposal_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_proposals are immutable (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_proposals_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_proposals
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_proposal_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_plan_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_plans are immutable (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_plans_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_plans
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_plan_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_proposal_journal_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_proposal_journal is append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_proposal_journal_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_proposal_journal
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_proposal_journal_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_strategy_proposal_journal_immutable ON public.strategy_proposal_journal")
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_proposal_journal_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_strategy_plans_immutable ON public.strategy_plans")
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_plan_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_strategy_proposals_immutable ON public.strategy_proposals")
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_proposal_mutation()")
    op.drop_index("idx_proposal_journal_strategy", table_name="strategy_proposal_journal")
    op.drop_table("strategy_proposal_journal")
    op.drop_index("idx_plans_strategy", table_name="strategy_plans")
    op.drop_table("strategy_plans")
    op.drop_index("idx_proposals_strategy", table_name="strategy_proposals")
    op.drop_table("strategy_proposals")
