"""Admission policies, durable reservations, approvals, reconciliation version.

Revision ID: 20260917_000028
Revises: 20260917_000027
Create Date: 2026-09-17

Purely additive: five new tables plus one insert-only trigger. Nothing existing
is altered.

Why each table exists (R3 §8, §6):

- ``strategy_admission_policies`` is the **recorded basis** for every admission
  decision. ``allocation_inr`` is REQUIRED for a live strategy (service-enforced,
  because a paper-only strategy may legitimately have none): capital enforcement
  without a recorded allocation would be unenforceable the moment someone asked
  what the limit was. A NULL axis means "not enforced", which is different from
  zero and must stay distinguishable.

- ``strategy_reservations`` is the durable capacity claim. ``UNIQUE (plan_id)``
  makes one plan claim capacity once, ever. The row is mutable because a
  reservation has a lifecycle, so — unlike every other table in this campaign —
  it does NOT carry an insert-only trigger. What is insert-only is the **event
  log** beside it: ``strategy_reservation_events`` records every transition, so
  the sequence is append-only even though the status is not. ``reserved_notional_inr
  >= 0`` and the composite FK to ``strategies (id, account_scope)`` keep the
  claim honest about whose capacity it is.

- ``strategy_approvals`` is the owner's authorisation bound to an immutable plan
  and every structural pin that must still hold when execution starts. The
  partial unique index ``uq_approvals_plan_active`` (WHERE status = 'active') is
  the real contract: at most one live approval per plan, enforced by the
  database, so a concurrent double-approval cannot produce two. Superseding is a
  NEW row with the old one marked superseded — approvals are never rewritten.

- ``account_reconciliation_versions`` is the counter an approval pins so that
  divergence discovered *after* approval invalidates it. It is bumped inside
  ``reconcile_account`` in the same transaction as the state write, so the
  version can never disagree with the classification it describes.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000028"
down_revision = "20260917_000027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_admission_policies",
        sa.Column("strategy_id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("allocation_inr", sa.Double(), nullable=True),
        sa.Column("per_instrument_notional_inr", sa.Double(), nullable=True),
        sa.Column("gross_notional_inr", sa.Double(), nullable=True),
        sa.Column("max_open_instruments", sa.Integer(), nullable=True),
        sa.Column("admissions_per_window", sa.Integer(), nullable=True),
        sa.Column("admission_window_seconds", sa.Integer(), nullable=True),
        sa.Column("daily_loss_budget_inr", sa.Double(), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "allocation_inr IS NULL OR allocation_inr >= 0",
            name="ck_sap_allocation_non_negative",
        ),
        sa.CheckConstraint(
            "admissions_per_window IS NULL OR admissions_per_window > 0",
            name="ck_sap_admissions_per_window",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["public.strategies.id", "public.strategies.account_scope"],
            name="strategy_admission_policies_strategy_id_account_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "strategy_reservations",
        sa.Column(
            "reservation_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("evaluation_id", sa.Text(), nullable=False),
        sa.Column("execution_environment", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("reserved_notional_inr", sa.Double(), nullable=False),
        sa.Column("margin_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("margin_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_res_execution_environment",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'renewed', 'consumed', 'released', 'expired', "
            "'action_required')",
            name="ck_res_status",
        ),
        sa.CheckConstraint("reserved_notional_inr >= 0", name="ck_res_notional_non_negative"),
        sa.UniqueConstraint("plan_id", name="uq_reservations_plan"),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["public.strategy_plans.plan_id"],
            name="strategy_reservations_plan_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["public.strategies.id", "public.strategies.account_scope"],
            name="strategy_reservations_strategy_id_account_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_reservations_account_status",
        "strategy_reservations",
        ["account_id", "status"],
    )

    op.create_table(
        "strategy_reservation_events",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
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
            "event IN ('created', 'renewed', 'advanced', 'consumed', 'released', "
            "'expired', 'action_required', 'disposition_confirmed')",
            name="ck_res_event",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["public.strategy_reservations.reservation_id"],
            name="strategy_reservation_events_reservation_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_reservation_events",
        "strategy_reservation_events",
        ["reservation_id", "created_at"],
    )

    op.create_table(
        "strategy_approvals",
        sa.Column(
            "approval_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("exposure_snapshot_version", sa.BigInteger(), nullable=False),
        sa.Column("exposure_snapshot_hash", sa.Text(), nullable=True),
        sa.Column("reconciliation_version", sa.BigInteger(), nullable=False),
        sa.Column("catalog_generation", sa.UUID(), nullable=False),
        sa.Column(
            "session_product_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'superseded', 'revoked')",
            name="ck_appr_status",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["public.strategy_plans.plan_id"],
            name="strategy_approvals_plan_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["public.strategy_reservations.reservation_id"],
            name="strategy_approvals_reservation_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["public.strategies.id", "public.strategies.account_scope"],
            name="strategy_approvals_strategy_id_account_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    # At most ONE active approval per plan, enforced by the database: a concurrent
    # double-approval resolves to a unique violation, never to two live approvals.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_approvals_plan_active
            ON public.strategy_approvals (plan_id)
            WHERE status = 'active';
        """
    )
    op.create_index(
        "idx_approvals_strategy", "strategy_approvals", ["strategy_id", "created_at"]
    )

    op.create_table(
        "account_reconciliation_versions",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_reservation_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_reservation_events are append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_reservation_events_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_reservation_events
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_reservation_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_reservation_events_immutable "
        "ON public.strategy_reservation_events"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_reservation_event_mutation()")
    op.drop_table("account_reconciliation_versions")
    op.drop_index("idx_approvals_strategy", table_name="strategy_approvals")
    op.execute("DROP INDEX IF EXISTS public.uq_approvals_plan_active")
    op.drop_table("strategy_approvals")
    op.drop_index("idx_reservation_events", table_name="strategy_reservation_events")
    op.drop_table("strategy_reservation_events")
    op.drop_index("idx_reservations_account_status", table_name="strategy_reservations")
    op.drop_table("strategy_reservations")
    op.drop_table("strategy_admission_policies")
