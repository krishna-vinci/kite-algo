"""Strategy account truth: ingested fill facts, ingest state, reconciliation
state and append-only attribution adjustments.

Revision ID: 20260917_000026
Revises: 20260917_000025
Create Date: 2026-09-17

Purely additive: nothing existing is altered. Five new tables plus two
insert-only triggers.

Why each table exists:

- ``broker_trade_facts`` is the account-wide, broker-identity-keyed record of
  every fill on an account — tracked *and* untracked orders. Deduplicated by
  ``(account_id, trade_id)`` so re-ingestion is a no-op, and insert-only so a
  bad ingest generation is corrected by ingesting the missing truth, never by
  editing history.
- ``account_ingest_state`` carries the ingest generation/cursor so divergence
  classification can tell "we have not looked yet" from "we looked and the
  mismatch is real" (R3 §17 ingestion-lag classification).
- ``strategy_reconciliation_state`` persists the per-coordinate classification
  (``aligned`` / ``pending_ingest`` / ``unexplained``) plus the bounded refresh
  counter and the single owner escalation, so the freeze and the UI read
  persisted state rather than recomputing inline.
- ``strategy_attribution_adjustments`` (+ ``_lines``) is the append-only
  reclassification record: a line moves signed quantity between the manual
  residual and exactly one canonical strategy book. Trigger-immutable, and the
  composite FK to ``strategies (id, owner_id, account_scope)`` mirrors
  ``strategy_run_bindings`` so owner/account drift is database-refused.

Adjustment lines are **live-book facts in V1**: there is deliberately no
``execution_environment`` column, and the fold assigns ``environment='live'``.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260917_000026"
down_revision = "20260917_000025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_trade_facts",
        sa.Column("fact_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("trade_id", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("tradingsymbol", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("transaction_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("fill_price", sa.Double(), nullable=True),
        sa.Column("trade_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingest_generation", sa.BigInteger(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "trade_id", name="uq_broker_trade_facts_account_trade"),
        sa.CheckConstraint("transaction_type IN ('BUY', 'SELL')", name="ck_btf_transaction_type"),
        sa.CheckConstraint("quantity > 0", name="ck_btf_quantity"),
    )
    op.create_index(
        "idx_btf_account_coord",
        "broker_trade_facts",
        ["account_id", "instrument_token", "exchange", "tradingsymbol", "product"],
    )

    op.create_table(
        "account_ingest_state",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("last_orders_fetch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_complete_ingest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingest_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('idle', 'refreshing', 'stale')", name="ck_ais_status"),
    )

    op.create_table(
        "strategy_reconciliation_state",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("instrument_token", sa.BigInteger(), primary_key=True),
        sa.Column("exchange", sa.Text(), primary_key=True),
        sa.Column("tradingsymbol", sa.Text(), primary_key=True),
        sa.Column("product", sa.Text(), primary_key=True),
        sa.Column("divergence_class", sa.Text(), nullable=False),
        sa.Column("broker_quantity", sa.BigInteger(), nullable=False),
        sa.Column("attributed_quantity", sa.BigInteger(), nullable=False),
        sa.Column("manual_quantity", sa.BigInteger(), nullable=False),
        sa.Column("residual_quantity", sa.BigInteger(), nullable=False),
        sa.Column("refresh_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("owner_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "divergence_class IN ('aligned', 'pending_ingest', 'unexplained')",
            name="ck_srs_divergence_class",
        ),
    )

    op.create_table(
        "strategy_attribution_adjustments",
        sa.Column("adjustment_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("adjustment_kind", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "adjustment_kind IN ('owner_reclassification')", name="ck_saa_adjustment_kind"
        ),
    )
    op.create_index(
        "idx_saa_account", "strategy_attribution_adjustments", ["account_id", "created_at"]
    )

    op.create_table(
        "strategy_attribution_adjustment_lines",
        sa.Column("adjustment_id", sa.UUID(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("tradingsymbol", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("adjustment_id", "line_no"),
        sa.CheckConstraint("quantity_delta <> 0", name="ck_saal_quantity_delta"),
        sa.ForeignKeyConstraint(
            ["adjustment_id"],
            ["strategy_attribution_adjustments.adjustment_id"],
            ondelete="RESTRICT",
        ),
        # Owner/account integrity mirrors strategy_run_bindings exactly: a line
        # cannot disagree with the canonical strategy it credits.
        sa.ForeignKeyConstraint(
            ["strategy_id", "owner_id", "account_id"],
            ["strategies.id", "strategies.owner_id", "strategies.account_scope"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_saal_strategy_coord",
        "strategy_attribution_adjustment_lines",
        ["strategy_id", "instrument_token", "exchange", "tradingsymbol", "product"],
    )

    # Insert-only enforcement, same pattern as strategy_run_bindings.
    op.execute(
        """
        CREATE FUNCTION forbid_broker_trade_fact_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'broker_trade_facts are immutable (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_broker_trade_facts_immutable
        BEFORE UPDATE OR DELETE ON public.broker_trade_facts
        FOR EACH ROW EXECUTE FUNCTION forbid_broker_trade_fact_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION forbid_strategy_attribution_adjustment_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_attribution_adjustments are immutable (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_attribution_adjustments_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_attribution_adjustments
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_attribution_adjustment_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_strategy_attribution_adjustment_lines_immutable
        BEFORE UPDATE OR DELETE ON public.strategy_attribution_adjustment_lines
        FOR EACH ROW EXECUTE FUNCTION forbid_strategy_attribution_adjustment_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_attribution_adjustment_lines_immutable "
        "ON public.strategy_attribution_adjustment_lines"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_attribution_adjustments_immutable "
        "ON public.strategy_attribution_adjustments"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_broker_trade_facts_immutable ON public.broker_trade_facts"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_strategy_attribution_adjustment_mutation()")
    op.execute("DROP FUNCTION IF EXISTS forbid_broker_trade_fact_mutation()")
    op.drop_index("idx_saal_strategy_coord", table_name="strategy_attribution_adjustment_lines")
    op.drop_table("strategy_attribution_adjustment_lines")
    op.drop_index("idx_saa_account", table_name="strategy_attribution_adjustments")
    op.drop_table("strategy_attribution_adjustments")
    op.drop_table("strategy_reconciliation_state")
    op.drop_table("account_ingest_state")
    op.drop_index("idx_btf_account_coord", table_name="broker_trade_facts")
    op.drop_table("broker_trade_facts")
