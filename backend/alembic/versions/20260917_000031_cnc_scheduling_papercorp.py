"""CNC scheduling, paper partial fills, corporate-action detection.

Revision ID: 20260917_000031
Revises: 20260917_000030
Create Date: 2026-09-17

Additive in effect: three new tables, one append-only child log, and a widening
of an existing CHECK so the stored schedule table can carry the new kinds.
Nothing existing is dropped, rewritten, or invalidated.

Why each table exists (R3 §11, §18; G11/G12/G13):

- ``strategy_schedule_occurrences`` is the durable record of *which* occurrences
  of a schedule were materialised and what happened to each. ``UNIQUE
  (schedule_id, occurrence_key)`` is the fencing contract: two schedulers
  racing the same tick collide on the index instead of double-firing, and a
  missed occurrence is a row whose status says ``skipped`` with its reason
  journalled — never a silent gap.

- ``paper_order_fill_progress`` carries a paper order's fill state, because
  until now every paper fill was instant and full. Keeping it in a side table
  rather than altering ``paper_orders`` means the existing runtime is untouched:
  an order with no progress row behaves exactly as before.

- ``strategy_corporate_action_events`` (+ its append-only ``..._log`` child) is
  the detection record. A broker split that would otherwise be absorbed silently
  becomes a fact: the parent row is MUTABLE because a detection has a lifecycle
  (detected → escalated → resolved), and the child log is insert-only so the
  sequence cannot be rewritten. This mirrors ``strategy_reservations`` +
  ``strategy_reservation_events`` exactly — one pattern, not half of two.

- ``hosted_strategy_schedules`` gains ``monthly``/``calendar`` kinds plus the two
  configuration columns they need. The kinds are admitted by REPLACING the CHECK
  (there is no way to widen a CHECK in place), which is the same repair precedent
  as ``universes_kind_check`` in ``20260911_000016``: a superset constraint
  cannot invalidate an existing row. No existing row is read or written.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000031"
down_revision = "20260917_000030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- the existing schedule table gains the new kinds and their config -----
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_kind;"
    )
    op.execute(
        """
        ALTER TABLE public.hosted_strategy_schedules
            ADD CONSTRAINT ck_hosted_strategy_schedules_kind
            CHECK (schedule_kind IN ('daily', 'weekly', 'monthly', 'calendar'));
        """
    )
    op.add_column(
        "hosted_strategy_schedules",
        sa.Column("day_of_month", sa.Integer(), nullable=True),
    )
    op.add_column(
        "hosted_strategy_schedules",
        sa.Column("calendar_dates", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "ck_hosted_strategy_schedules_day_of_month",
        "hosted_strategy_schedules",
        "day_of_month IS NULL OR (day_of_month >= 1 AND day_of_month <= 31)",
    )
    op.create_check_constraint(
        "ck_hosted_strategy_schedules_monthly_day",
        "hosted_strategy_schedules",
        "schedule_kind <> 'monthly' OR day_of_month IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_hosted_strategy_schedules_calendar_dates",
        "hosted_strategy_schedules",
        "schedule_kind <> 'calendar' OR calendar_dates IS NOT NULL",
    )

    # -- new tables ----------------------------------------------------------
    op.create_table(
        "strategy_schedule_occurrences",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("schedule_id", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("occurrence_key", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluation_id", sa.Text(), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'fired', 'skipped', 'expired')",
            name="ck_sched_occurrence_status",
        ),
        sa.UniqueConstraint(
            "schedule_id", "occurrence_key", name="uq_schedule_occurrences_key"
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["public.hosted_strategy_schedules.id"],
            name="strategy_schedule_occurrences_schedule_id_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_schedule_occurrences_status",
        "strategy_schedule_occurrences",
        ["status", "due_at"],
    )

    op.create_table(
        "paper_order_fill_progress",
        sa.Column("account_scope", sa.Text(), primary_key=True),
        sa.Column("paper_order_id", sa.Text(), primary_key=True),
        sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('open', 'partially_filled', 'filled', 'cancelled')",
            name="ck_pofp_status",
        ),
        sa.CheckConstraint("filled_quantity >= 0", name="ck_pofp_filled_non_negative"),
        sa.CheckConstraint("remaining_quantity >= 0", name="ck_pofp_remaining_non_negative"),
    )

    op.create_table(
        "strategy_corporate_action_events",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("tradingsymbol", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="detected"),
        sa.Column("resolved_adjustment_id", sa.UUID(), nullable=True),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action_kind IN ('suspected_split', 'suspected_bonus', 'suspected_merger', "
            "'unclassified')",
            name="ck_scae_action_kind",
        ),
        sa.CheckConstraint(
            "status IN ('detected', 'escalated', 'resolved')", name="ck_scae_status"
        ),
    )
    op.create_index(
        "idx_corporate_action_account",
        "strategy_corporate_action_events",
        ["account_id", "detected_at"],
    )

    op.create_table(
        "strategy_corporate_action_event_log",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column(
            "detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "event IN ('detected', 'escalated', 'freeze_confirmed', 'resolved')",
            name="ck_scael_event",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["public.strategy_corporate_action_events.id"],
            name="strategy_corporate_action_event_log_event_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_corporate_action_log_event",
        "strategy_corporate_action_event_log",
        ["event_id", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_corporate_action_log_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_corporate_action_event_log is append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_corporate_action_log_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_corporate_action_event_log
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_corporate_action_log_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_corporate_action_log_immutable "
        "ON public.strategy_corporate_action_event_log"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_corporate_action_log_mutation()")
    op.drop_index("idx_corporate_action_log_event", table_name="strategy_corporate_action_event_log")
    op.drop_table("strategy_corporate_action_event_log")
    op.drop_index("idx_corporate_action_account", table_name="strategy_corporate_action_events")
    op.drop_table("strategy_corporate_action_events")
    op.drop_table("paper_order_fill_progress")
    op.drop_index("idx_schedule_occurrences_status", table_name="strategy_schedule_occurrences")
    op.drop_table("strategy_schedule_occurrences")

    # Restore the original schedule vocabulary only after the new-kind rows are
    # gone, so the narrower CHECK can never reject an existing row.
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_monthly_day;"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_calendar_dates;"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_day_of_month;"
    )
    op.drop_column("hosted_strategy_schedules", "calendar_dates")
    op.drop_column("hosted_strategy_schedules", "day_of_month")
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_kind;"
    )
    op.execute(
        """
        ALTER TABLE public.hosted_strategy_schedules
            ADD CONSTRAINT ck_hosted_strategy_schedules_kind
            CHECK (schedule_kind IN ('daily', 'weekly'));
        """
    )
