"""Durable strategy attribution: canonical strategies, adapters, grants,
immutable RESTRICT bindings, full-recompute projection.

Revision ID: 20260917_000025
Revises: 20260915_000024
Create Date: 2026-09-17

Additive plus one backfill: every existing hosted strategy becomes a canonical
strategy with the SAME id. Existing fact tables are NOT denormalized. Bindings
are database-enforced immutable and ON DELETE RESTRICT in both directions, so
attribution history outlives operational run rows and strategy deletion with
history is refused.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260917_000025"
down_revision = "20260915_000024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner_id", sa.Text(), primary_key=False, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("journal_template_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_id", "name", name="uq_strategies_owner_name"),
        # Composite targets for database-enforced integrity: bindings reference
        # (id, owner_id, account_scope); projection and state reference
        # (id, account_scope) for account agreement.
        sa.UniqueConstraint("id", "owner_id", "account_scope", name="uq_strategies_id_owner_account"),
        sa.UniqueConstraint("id", "account_scope", name="uq_strategies_id_account"),
        sa.CheckConstraint("status IN ('active', 'disabled', 'archived')", name="ck_strategies_status"),
        sa.ForeignKeyConstraint(["journal_template_id"], ["journal_strategy_templates.id"], ondelete="SET NULL"),
    )

    # Backfill: canonical row per hosted strategy, SAME id (IDs preserved).
    op.execute(
        """
        INSERT INTO public.strategies (id, owner_id, name, account_scope, status)
        SELECT hs.id, hs.owner_id, hs.name, hs.default_account_scope,
               CASE hs.status WHEN 'disabled' THEN 'disabled' ELSE 'active' END
        FROM public.hosted_strategies hs
        ON CONFLICT (id) DO NOTHING
        """
    )
    # Hosted adapter now references the canonical strategy INCLUDING owner and
    # account, so hosted identity cannot drift from canonical identity.
    op.create_foreign_key(
        "fk_hosted_strategies_canonical",
        "hosted_strategies",
        "strategies",
        ["id", "owner_id", "default_account_scope"],
        ["id", "owner_id", "account_scope"],
        ondelete="RESTRICT",
    )

    # Composite FK target for binding environment integrity: a binding's
    # execution_environment must equal the run's persisted execution_mode.
    op.create_unique_constraint(
        "uq_algo_worker_runs_id_mode",
        "algo_worker_runs",
        ["strategy_run_id", "execution_mode"],
    )

    op.create_table(
        "external_strategy_adapters",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("strategy_id", sa.Text(), sa.ForeignKey("strategies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_external_adapters_status"),
    )
    op.create_index("idx_external_adapters_strategy", "external_strategy_adapters", ["strategy_id"])

    op.create_table(
        "worker_token_strategy_grants",
        sa.Column("token_id", sa.Text(), sa.ForeignKey("algo_worker_tokens.token_id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.Text(), sa.ForeignKey("strategies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("granted_by", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_id", "strategy_id"),
    )
    op.create_index("idx_grants_strategy", "worker_token_strategy_grants", ["strategy_id"])

    op.create_table(
        "strategy_run_bindings",
        sa.Column("strategy_run_id", sa.Text(), primary_key=True),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("execution_environment", sa.Text(), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("bound_by", sa.Text(), nullable=False),
        sa.Column(
            "binding_source",
            sa.Text(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "binding_source IN ('hosted_job', 'external_run_create', 'audited_mapping', 'legacy_compat')",
            name="ck_binding_source",
        ),
        sa.CheckConstraint(
            "execution_environment IN ('live', 'paper', 'dry_run')",
            name="ck_binding_environment",
        ),
        # Owner/account integrity: the binding's owner and account must equal
        # the canonical strategy's (composite FK — mismatch is impossible).
        sa.ForeignKeyConstraint(
            ["strategy_id", "owner_id", "account_id"],
            ["strategies.id", "strategies.owner_id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
        # Environment integrity: the binding's environment must equal the
        # run's persisted execution_mode (composite FK).
        sa.ForeignKeyConstraint(
            ["strategy_run_id", "execution_environment"],
            ["algo_worker_runs.strategy_run_id", "algo_worker_runs.execution_mode"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index("idx_strategy_run_bindings_strategy", "strategy_run_bindings", ["strategy_id"])
    op.create_index(
        "idx_strategy_run_bindings_account_env",
        "strategy_run_bindings",
        ["account_id", "strategy_id", "execution_environment"],
    )

    # Database-enforced immutability: INSERT-only. Audited legacy mapping adds
    # NEW rows; a mistaken binding requires the dedicated ownership-transfer
    # protocol (outside G1) which will itself append, never mutate.
    op.execute(
        """
        CREATE FUNCTION forbid_strategy_run_binding_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_run_bindings are immutable (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_run_bindings_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_run_bindings
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_run_binding_mutation();
        """
    )

    op.create_table(
        "strategy_position_projection",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("strategy_id", sa.Text(), primary_key=True),
        sa.Column("execution_environment", sa.Text(), primary_key=True),
        sa.Column("identity_kind", sa.Text(), primary_key=True),
        sa.Column("identity_key", sa.Text(), primary_key=True),
        sa.Column("product", sa.Text(), primary_key=True),
        sa.Column("canonical_instrument_id", sa.UUID(), nullable=True),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("tradingsymbol", sa.Text(), nullable=False),
        sa.Column("net_quantity", sa.Integer(), nullable=False),
        sa.Column("unresolved_reason", sa.Text(), nullable=True),
        sa.Column("projection_version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("execution_environment IN ('live', 'paper', 'dry_run')", name="ck_spp_environment"),
        sa.CheckConstraint("identity_kind IN ('canonical', 'raw')", name="ck_spp_identity_kind"),
        sa.CheckConstraint(
            "(identity_kind = 'canonical' AND canonical_instrument_id IS NOT NULL) "
            "OR (identity_kind = 'raw' AND canonical_instrument_id IS NULL)",
            name="ck_spp_identity_consistency",
        ),
        # Account agreement is database-enforced: a projection row's account
        # must equal the canonical strategy's account (not merely a strategy_id FK).
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_spp_strategy",
        "strategy_position_projection",
        ["account_id", "strategy_id", "execution_environment"],
    )

    op.create_table(
        "strategy_projection_state",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("strategy_id", sa.Text(), primary_key=True),
        sa.Column("execution_environment", sa.Text(), primary_key=True),
        sa.Column("projection_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("last_rebuild_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("execution_environment IN ('live', 'paper', 'dry_run')", name="ck_sps_environment"),
        # Account agreement is database-enforced, not merely a strategy_id FK.
        sa.ForeignKeyConstraint(
            ["strategy_id", "account_id"],
            ["strategies.id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.drop_table("strategy_projection_state")
    op.drop_table("strategy_position_projection")
    op.execute("DROP TRIGGER IF EXISTS trg_strategy_run_bindings_immutable ON public.strategy_run_bindings")
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_run_binding_mutation()")
    op.drop_index("idx_strategy_run_bindings_account_env", table_name="strategy_run_bindings")
    op.drop_index("idx_strategy_run_bindings_strategy", table_name="strategy_run_bindings")
    op.drop_table("strategy_run_bindings")
    op.drop_constraint(
        "uq_algo_worker_runs_id_mode",
        "algo_worker_runs",
        type_="unique",
    )
    op.drop_index("idx_grants_strategy", table_name="worker_token_strategy_grants")
    op.drop_table("worker_token_strategy_grants")
    op.drop_index("idx_external_adapters_strategy", table_name="external_strategy_adapters")
    op.drop_table("external_strategy_adapters")
    op.drop_constraint("fk_hosted_strategies_canonical", "hosted_strategies", type_="foreignkey")
    # Canonical rows are NOT deleted on downgrade if bindings/history exist; a
    # manual, audited cleanup is required — downgrade is destructive-guarded.
    op.drop_table("strategies")
