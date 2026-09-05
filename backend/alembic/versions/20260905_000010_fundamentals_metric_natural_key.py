"""make nullable fundamentals metric keys idempotent

Revision ID: 20260905_000010
Revises: 20260905_000009
Create Date: 2026-09-05 00:00:10
"""
from alembic import op


revision = "20260905_000010"
down_revision = "20260905_000009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ordinary UNIQUE constraints treat NULL values as distinct. Summary
    # metrics have no period_end, so retain the newest copy before replacing
    # the original constraint with PostgreSQL 15+'s NULL-safe form.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, statement_scope, dataset, period_end, metric_key
                       ORDER BY scraped_at DESC, id DESC
                   ) AS duplicate_rank
            FROM public.fundamentals_metrics
        )
        DELETE FROM public.fundamentals_metrics AS metrics
        USING ranked
        WHERE metrics.id = ranked.id
          AND ranked.duplicate_rank > 1
        """
    )
    op.execute(
        """
        ALTER TABLE public.fundamentals_metrics
        DROP CONSTRAINT IF EXISTS fundamentals_metrics_symbol_statement_scope_dataset_period__key
        """
    )
    op.execute(
        """
        ALTER TABLE public.fundamentals_metrics
        ADD CONSTRAINT uq_fundamentals_metrics_natural_key
        UNIQUE NULLS NOT DISTINCT (symbol, statement_scope, dataset, period_end, metric_key)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.fundamentals_metrics
        DROP CONSTRAINT IF EXISTS uq_fundamentals_metrics_natural_key
        """
    )
    op.execute(
        """
        ALTER TABLE public.fundamentals_metrics
        ADD CONSTRAINT fundamentals_metrics_symbol_statement_scope_dataset_period__key
        UNIQUE (symbol, statement_scope, dataset, period_end, metric_key)
        """
    )
