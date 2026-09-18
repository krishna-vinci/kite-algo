"""MIS square-off evidence.

Revision ID: 20260917_000032
Revises: 20260917_000031
Create Date: 2026-09-17

Purely additive: one table plus one insert-only trigger. Nothing existing is
altered.

Why this table exists (R3 §12, §16):

The platform owns MIS square-off timing — the schedule, the ``mis_squareoff_buffer``
rule and the durable exit claim path already exist and are deliberately NOT
changed here. What was missing is the *record*: when a square-off fired, what it
sized the exit to, and what happened. Without that, "the square-off ran" is an
assertion rather than evidence, and a failed square-off is indistinguishable from
one that never happened.

``strategy_squareoff_evidence`` is append-only because it is a ledger of what the
platform did, not state that gets edited. A failed square-off is recorded as
``action_required`` and keeps reconciling — it is explicitly NOT settlement
(R3 §16's four axes stay unsatisfied), and a broker auto-square-off observed
afterwards is recorded as ``missed_by_broker``: a fallback that happened, never
the control that decided.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000032"
down_revision = "20260917_000031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_squareoff_evidence",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("strategy_run_id", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_claim_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "outcome IN ('squared_off', 'action_required', 'missed_by_broker', "
            "'stale_worker_exit')",
            name="ck_sse_outcome",
        ),
    )
    op.create_index(
        "idx_sse_run", "strategy_squareoff_evidence", ["strategy_run_id", "session_date"]
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_squareoff_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_squareoff_evidence is append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_squareoff_evidence_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_squareoff_evidence
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_squareoff_evidence_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_squareoff_evidence_immutable "
        "ON public.strategy_squareoff_evidence"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_squareoff_evidence_mutation()")
    op.drop_index("idx_sse_run", table_name="strategy_squareoff_evidence")
    op.drop_table("strategy_squareoff_evidence")
