"""Option structures: structure config and settlement evidence.

Revision ID: 20260917_000034
Revises: 20260917_000033
Create Date: 2026-09-17

Additive: two columns plus one table and its insert-only trigger. Nothing existing
is altered, and **no CHECK is widened** — see below.

The plan's sketch asks for the option-run state CHECK to be widened to include
``settled``. That step is a no-op in this source: ``option_run_states.status`` is a
plain ``VARCHAR(64) NOT NULL`` with **no CHECK constraint** (verified in
``backend/schema.sql`` and across the migrations), and the only status CHECK in the
option tables is on ``option_strategy_runs.status``, which is a different table with
a different four-value vocabulary. ``settled`` is therefore a Python-vocabulary
addition on ``OptionRunStatus`` plus a settlement-adapter registration, not a DDL
change. Recording it here because a migration that "widens" a constraint which does
not exist would be misleading to the next reader.

``structure_digest`` and ``expiry_policy`` live on ``option_run_states`` because that
is the run record the runtime reads and writes, keyed by the TEXT
``strategy_run_id`` that ``option_settlement_evidence.option_run_id`` references.

``option_settlement_evidence`` is append-only because settlement is a claim about
what actually happened at the exchange, and a claim that can be edited is not
evidence. Cash settlement applies an external adjustment only with a row here: never
inferred from expiry time or from a position disappearing, because both of those are
consistent with the position still existing and simply not being visible.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260917_000034"
down_revision = "20260917_000033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "option_run_states", sa.Column("structure_digest", sa.Text(), nullable=True)
    )
    op.add_column(
        "option_run_states", sa.Column("expiry_policy", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_option_run_states_expiry_policy",
        "option_run_states",
        "expiry_policy IS NULL OR expiry_policy IN "
        "('exit_before_cutoff', 'allow_cash_settlement', 'allow_physical_settlement')",
    )

    op.create_table(
        "option_settlement_evidence",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("option_run_id", sa.Text(), nullable=False),
        sa.Column("structure_digest", sa.Text(), nullable=False),
        sa.Column("settlement_kind", sa.Text(), nullable=False),
        sa.Column("evidence_source", sa.Text(), nullable=False),
        sa.Column("evidence_ref", postgresql.JSONB(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("adjustment_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "settlement_kind IN ('cash', 'physical')", name="ck_ose_settlement_kind"
        ),
        # Only authoritative sources. \"The position disappeared\" is not one of them.
        sa.CheckConstraint(
            "evidence_source IN ('broker_ledger', 'contract_note', 'exchange_file')",
            name="ck_ose_evidence_source",
        ),
    )
    op.create_index(
        "idx_ose_run", "option_settlement_evidence", ["option_run_id"]
    )

    op.execute(
        """
        CREATE FUNCTION forbid_option_settlement_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'option_settlement_evidence is append-only (insert-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_option_settlement_evidence_immutable
        BEFORE UPDATE OR DELETE ON public.option_settlement_evidence
        FOR EACH ROW EXECUTE FUNCTION forbid_option_settlement_evidence_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_option_settlement_evidence_immutable "
        "ON public.option_settlement_evidence"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_option_settlement_evidence_mutation()")
    op.drop_index("idx_ose_run", table_name="option_settlement_evidence")
    op.drop_table("option_settlement_evidence")
    op.drop_constraint(
        "ck_option_run_states_expiry_policy", "option_run_states", type_="check"
    )
    op.drop_column("option_run_states", "expiry_policy")
    op.drop_column("option_run_states", "structure_digest")
