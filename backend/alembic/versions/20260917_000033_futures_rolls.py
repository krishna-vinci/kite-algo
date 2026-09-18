"""Futures rolls.

Revision ID: 20260917_000033
Revises: 20260917_000032
Create Date: 2026-09-17

Purely additive: two tables plus one insert-only trigger. Nothing existing is
altered.

Why these tables exist (R3 §13, locked decision 6):

A roll is not two orders and it is not a basket. It is an **ordered** transition
with a rule that no order-level mechanism can express: the old contract's close
step is released only once the FULL required replacement quantity is *proven*
filled, where proof means the strategy's attributed book on the new contract, not
an order-status label. A partial replacement stalls the roll at
``action_required`` with the old attribution intact and never auto-reverses.

``strategy_rolls`` is therefore mutable — a roll has a lifecycle — while
``strategy_roll_events`` is the append-only record of it, the same pattern as
``strategy_reservations`` and ``strategy_corporate_action_events`` rather than a
third one. Both instrument identities are retained as NOT NULL columns through the
whole transition, because a roll that forgot which contract it came from could not
prove the old book flat and could not be audited afterwards.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000033"
down_revision = "20260917_000032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_rolls",
        sa.Column(
            "roll_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        # BOTH identities, retained through the transition: a roll that forgot
        # where it came from could not prove the old book flat.
        sa.Column("old_instrument_id", sa.Text(), nullable=False),
        sa.Column("new_instrument_id", sa.Text(), nullable=False),
        sa.Column("old_coordinate", postgresql.JSONB(), nullable=False),
        sa.Column("new_coordinate", postgresql.JSONB(), nullable=False),
        sa.Column("required_replacement_quantity", sa.Integer(), nullable=False),
        sa.Column("proven_filled_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.Text(), nullable=False, server_default="acquiring"),
        sa.Column("action_reason", sa.Text(), nullable=True),
        sa.Column("peak_margin_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("plan_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('acquiring', 'proving_filled', 'releasing_old', 'completed', "
            "'action_required')",
            name="ck_roll_state",
        ),
        sa.CheckConstraint(
            "required_replacement_quantity > 0", name="ck_roll_required_positive"
        ),
        sa.CheckConstraint("proven_filled_quantity >= 0", name="ck_roll_proven_non_negative"),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["public.strategy_plans.plan_id"],
            name="strategy_rolls_plan_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["public.strategies.id", "public.strategies.account_scope"],
            name="strategy_rolls_strategy_id_account_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_rolls_strategy_state", "strategy_rolls", ["strategy_id", "state"]
    )

    op.create_table(
        "strategy_roll_events",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("roll_id", sa.UUID(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column(
            "detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "event IN ('created', 'acquired', 'fill_proven', 'close_released', 'old_flat', "
            "'completed', 'stalled', 'escalated')",
            name="ck_roll_event",
        ),
        sa.ForeignKeyConstraint(
            ["roll_id"],
            ["public.strategy_rolls.roll_id"],
            name="strategy_roll_events_roll_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("idx_roll_events", "strategy_roll_events", ["roll_id", "created_at"])

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_roll_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_roll_events are append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_roll_events_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_roll_events
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_roll_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_roll_events_immutable "
        "ON public.strategy_roll_events"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_roll_event_mutation()")
    op.drop_index("idx_roll_events", table_name="strategy_roll_events")
    op.drop_table("strategy_roll_events")
    op.drop_index("idx_rolls_strategy_state", table_name="strategy_rolls")
    op.drop_table("strategy_rolls")
