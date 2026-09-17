"""Execution-quiescence barrier and four-axis settlement evidence (G7).

Revision ID: 20260917_000029
Revises: 20260917_000028
Create Date: 2026-09-17

Purely additive: three new tables plus two insert-only triggers. Nothing
existing is altered.

Why each table exists (R3 §16, plan D-1/D-3/D-5):

- ``strategy_execution_barriers`` is the durable execution version per book
  ``(account_id, strategy_id, execution_environment)``. Quiescence is NEVER
  inferred from a quiet window or two identical reads: a work transition bumps
  ``barrier_version`` in the same transaction as its event row, and a recorded
  proof pins ``quiet_since_version = barrier_version`` — so any later work
  event invalidates every prior proof by construction
  (``quiet_since_version <> barrier_version``).

- ``strategy_execution_barrier_events`` is the append-only event log of the
  barrier (``work_created`` / ``work_resolved`` / ``proof_recorded``). The
  version recorded on each event is the barrier version at the moment of the
  event; a proof's event row does NOT bump the version — proofs do not change
  the version, work does. Insert-only: history is never rewritten.

- ``strategy_settlement_assessments`` is the append-only snapshot of one
  four-axis assessment (R3 §16: quiescence, attribution-scoped flatness,
  terminal domain state, no live evaluation authority). An assessment is a
  snapshot, not a state: it records ``barrier_version`` and per-axis digests so
  a later barrier bump makes its staleness *detectable* — the platform
  re-assesses before acting on any released evidence.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000029"
down_revision = "20260917_000028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_execution_barriers",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("strategy_id", sa.Text(), primary_key=True),
        sa.Column(
            "execution_environment",
            sa.Text(),
            primary_key=True,
        ),
        sa.Column("barrier_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("quiet_since_version", sa.BigInteger(), nullable=True),
        sa.Column("last_proof_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_seb_environment",
        ),
    )

    op.create_table(
        "strategy_execution_barrier_events",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("execution_environment", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
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
            "event IN ('work_created', 'work_resolved', 'proof_recorded')",
            name="ck_sebe_event",
        ),
        sa.CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_sebe_environment",
        ),
    )
    op.create_index(
        "idx_barrier_events_key",
        "strategy_execution_barrier_events",
        ["account_id", "strategy_id", "execution_environment", "created_at"],
    )
    op.create_index(
        "idx_barrier_events_version",
        "strategy_execution_barrier_events",
        ["account_id", "strategy_id", "execution_environment", "version"],
    )

    op.create_table(
        "strategy_settlement_assessments",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("execution_environment", sa.Text(), nullable=False),
        sa.Column("overall", sa.Text(), nullable=False),
        sa.Column("barrier_version", sa.BigInteger(), nullable=False),
        sa.Column("axes", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "overall IN ('settled', 'unsettled', 'unknown')",
            name="ck_ssa_overall",
        ),
        sa.CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_ssa_environment",
        ),
    )
    op.create_index(
        "idx_settlement_assessments_key",
        "strategy_settlement_assessments",
        ["account_id", "strategy_id", "execution_environment", "created_at"],
    )

    # Append-only enforcement for both event surfaces: a proof, a work event or
    # an assessment is history, and rewriting history would make "settled" a
    # state instead of the snapshot D-5 demands.
    op.execute(
        """
        CREATE FUNCTION forbid_strategy_barrier_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_execution_barrier_events are append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_barrier_events_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_execution_barrier_events
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_barrier_event_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION forbid_settlement_assessment_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_settlement_assessments are append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_settlement_assessments_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_settlement_assessments
        FOR EACH ROW EXECUTE FUNCTION forbid_settlement_assessment_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_settlement_assessments_immutable "
        "ON public.strategy_settlement_assessments"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_settlement_assessment_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_barrier_events_immutable "
        "ON public.strategy_execution_barrier_events"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_barrier_event_mutation()")
    op.drop_index(
        "idx_settlement_assessments_key", table_name="strategy_settlement_assessments"
    )
    op.drop_table("strategy_settlement_assessments")
    op.drop_index(
        "idx_barrier_events_version", table_name="strategy_execution_barrier_events"
    )
    op.drop_index(
        "idx_barrier_events_key", table_name="strategy_execution_barrier_events"
    )
    op.drop_table("strategy_execution_barrier_events")
    op.drop_table("strategy_execution_barriers")
